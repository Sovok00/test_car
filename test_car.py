from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook
import pandas as pd
import requests
from xml.etree import ElementTree as ET
from botocore.config import Config
import os

# Отключаем проверку SSL для MinIO
os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2025, 5, 14),
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

dag = DAG(
    'test_task_car',
    default_args=default_args,
    description='Парсинг данных автомобилей и сохранение в PostgreSQL',
    schedule_interval='0 0 * * 1-6',  # Пн-Сб в 00:00
    catchup=False,
)

def get_currency_rate(date):
    """Получение курса USD от ЦБ РФ."""
    if date.weekday() == 5:  # Суббота -> берём курс за пятницу
        date = date - timedelta(days=1)

    url = f'https://cbr.ru/scripts/xml_daily.asp?date_req={date.strftime("%d/%m/%Y")}'
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for valute in root.findall('Valute'):
            if valute.find('CharCode').text == 'USD':
                return float(valute.find('Value').text.replace(',', '.')) / float(
                    valute.find('Nominal').text.replace(',', '.'))
    except Exception as e:
        raise Exception(f"Ошибка при получении курса: {str(e)}")

def process_file(**context):
    execution_date = context['execution_date']

    # Для старых версий Airflow (<2.4) используем такой подход
    s3_hook = S3Hook(aws_conn_id='aws_default')

    # Получаем клиента и вручную устанавливаем endpoint_url
    s3_client = s3_hook.get_conn()
    s3_client._endpoint.host = 'http://minio:9000'

    bucket_name = 'car-prices-bucket'
    file_key = f'cars_{execution_date.strftime("%Y%m%d")}.csv'

    try:
        # Проверяем существование файла
        if not s3_hook.check_for_key(file_key, bucket_name):
            raise ValueError(f"Файл {file_key} не найден в бакете {bucket_name}")

        # Читаем файл
        file_obj = s3_client.get_object(Bucket=bucket_name, Key=file_key)
        df = pd.read_csv(file_obj['Body'])

        # Обработка данных
        df['price_rub'] = df['price_foreign'] * get_currency_rate(execution_date)
        df['processing_date'] = execution_date

        context['ti'].xcom_push(key='processed_data', value=df.to_dict('records'))

    except Exception as e:
        context['ti'].xcom_push(key='error', value=str(e))
        raise

def load_to_postgres(**context):
    """Сохранение данных в PostgreSQL."""
    try:
        records = context['ti'].xcom_pull(task_ids='process_file', key='processed_data')
        if not records:
            error = context['ti'].xcom_pull(task_ids='process_file', key='error')
            raise ValueError(f"Нет данных для загрузки. Ошибка: {error}")

        # Используем PostgresHook вместо прямого подключения
        pg_hook = PostgresHook(postgres_conn_id='postgres_default')
        conn = pg_hook.get_conn()
        cursor = conn.cursor()

        # Создание таблицы (если не существует)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS car_prices (
            car_id SERIAL PRIMARY KEY,
            brand VARCHAR(100),
            model VARCHAR(100),
            engine_volume FLOAT,
            manufacture_year INTEGER,
            price_foreign FLOAT,
            price_rub FLOAT,
            file_actual_date DATE,
            processing_date TIMESTAMP,
            UNIQUE (brand, model, engine_volume, manufacture_year, file_actual_date)
        )
        """)

        # Пакетная вставка данных
        for record in records:
            cursor.execute("""
            INSERT INTO car_prices (brand, model, engine_volume, manufacture_year, 
                                  price_foreign, price_rub, file_actual_date, processing_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (brand, model, engine_volume, manufacture_year, file_actual_date) 
            DO UPDATE SET
                price_foreign = EXCLUDED.price_foreign,
                price_rub = EXCLUDED.price_rub,
                processing_date = EXCLUDED.processing_date
            """, (
                record['brand'],
                record['model'],
                record['engine_volume'],
                record['manufacture_year'],
                record['price_foreign'],
                record['price_rub'],
                record['file_actual_date'],
                record['processing_date']
            ))

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise Exception(f"Ошибка при загрузке в PostgreSQL: {str(e)}")
    finally:
        cursor.close()
        conn.close()

# Задачи с обработкой ошибок
process_task = PythonOperator(
    task_id='process_file',
    python_callable=process_file,
    provide_context=True,
    dag=dag,
    execution_timeout=timedelta(minutes=5),
)

load_task = PythonOperator(
    task_id='load_to_postgres',
    python_callable=load_to_postgres,
    provide_context=True,
    dag=dag,
    execution_timeout=timedelta(minutes=5),
)

process_task >> load_task