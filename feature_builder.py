#!/usr/bin/env python3
import argparse
import os
import psycopg2
from datetime import datetime


class OBTBuilder:
    def __init__(self, db_params):
        self.db_params = db_params
        self.conn = None
    
    def connect(self):
        self.conn = psycopg2.connect(**self.db_params)
        print("Conected to PostgreSQL")
    
    def disconnect(self):
        if self.conn:
            self.conn.close()
        print("Connection closed")
    
    def create_obt_table(self, analytics_table: str):
        with self.conn.cursor() as cursor:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {analytics_table} (
                    date TIMESTAMP,
                    ticker TEXT,
                    year INTEGER,
                    month INTEGER,
                    day_of_week INTEGER,
                    open DOUBLE PRECISION,
                    close DOUBLE PRECISION,
                    high DOUBLE PRECISION,
                    low DOUBLE PRECISION,
                    adj_close DOUBLE PRECISION,
                    prev_close DOUBLE PRECISION,
                    volume BIGINT,
                    return_close_open DOUBLE PRECISION,
                    return_prev_close DOUBLE PRECISION,
                    volatility_n_days DOUBLE PRECISION,
                    is_monday INTEGER, 
                    is_friday INTEGER, 
                    run_id TEXT,
                    ingested_at_utc TIMESTAMP DEFAULT now(),
                    source_name TEXT,
                    target_up INTEGER,
                    PRIMARY KEY (date, ticker)
                );
            """)
            self.conn.commit()
        print("OBT table ensured")

    def ensure_obt_columns(self, analytics_table: str):
        with self.conn.cursor() as cursor:
            cursor.execute(f"ALTER TABLE IF EXISTS {analytics_table} ADD COLUMN IF NOT EXISTS is_monday INTEGER;")
            cursor.execute(f"ALTER TABLE IF EXISTS {analytics_table} ADD COLUMN IF NOT EXISTS is_friday INTEGER;")
            cursor.execute(f"ALTER TABLE IF EXISTS {analytics_table} ADD COLUMN IF NOT EXISTS target_up INTEGER;")
            cursor.execute(f"ALTER TABLE IF EXISTS {analytics_table} ADD COLUMN IF NOT EXISTS ingested_at_utc TIMESTAMP DEFAULT now();")
            cursor.execute(f"ALTER TABLE IF EXISTS {analytics_table} ADD COLUMN IF NOT EXISTS source_name TEXT;")
        self.conn.commit()
        print(f"Tabla: {analytics_table}")


def main():
    parser = argparse.ArgumentParser(
        description='Financial Feature Builder - Construye features de trading desde datos raw',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  # Procesar todo el historial de AAPL
  python feature_builder.py --mode full --ticker AAPL --start-date 2020-01-01 --end-date 2024-12-01 --run-id daily_run --overwrite true

  # Procesar rango específico con ventana de volatilidad personalizada
  python feature_builder.py --mode by-date-range --ticker MSFT --start-date 2023-01-01 --end-date 2023-12-31 --run-id backfill_2023 --vol-window 30

Features generados:
  • return_close_open: Rendimiento intradiario (cierre vs apertura)
  • return_prev_close: Rendimiento diario (cierre actual vs cierre anterior)
  • volatility_n_days: Volatilidad histórica (std de returns en ventana móvil)
  • is_monday/is_friday: Indicadores de día de la semana
  • target_up: Variable objetivo (1 si cierre > apertura)
        """
    )

    parser.add_argument('--mode',
                       choices=['full','by-date-range'],
                       required=True,
                       help='Modo de procesamiento: full (todo el historial) o by-date-range (rango específico)')

    parser.add_argument('--ticker',
                       type=str,
                       required=True,
                       help='Símbolo bursátil a procesar (ej: AAPL, MSFT, GOOGL). Puede ser lista separada por comas')

    parser.add_argument('--start-date',
                       type=str,
                       required=True,
                       help='Fecha de inicio en formato YYYY-MM-DD')

    parser.add_argument('--end-date',
                       type=str,
                       required=True,
                       help='Fecha de fin en formato YYYY-MM-DD')

    parser.add_argument('--run-id',
                       type=str,
                       required=True,
                       help='Identificador único del proceso (ej: daily_run_20241213)')

    parser.add_argument('--overwrite',
                       type=str,
                       choices=['true','false'],
                       default='false',
                       help='Si sobrescribir registros existentes (true/false). Default: false')

    parser.add_argument('--vol-window',
                       type=int,
                       default=20,
                       help='Ventana para cálculo de volatilidad en días. Default: 20')

    args = parser.parse_args()

    db_params = {
        'host': os.getenv('PG_HOST'),
        'port': os.getenv('PG_PORT'),
        'database': os.getenv('PG_DB'),
        'user': os.getenv('PG_USER'),
        'password': os.getenv('PG_PASSWORD') 
    }

    raw_table = "raw." + (os.getenv('RAW_TABLE') or 'prices_daily')
    analytics_table = "analytics." + (os.getenv('ANALYTICS_TABLE') or 'daily_features')

    overwrite_flag = args.overwrite.lower() == 'true'
    full_mode = args.mode == 'full'
    #is_range_mode = args.mode == 'by-date-range'

    builder = OBTBuilder(db_params)
    builder.connect()
    builder.create_obt_table(analytics_table)
    builder.ensure_obt_columns(analytics_table)
    
    tickers = [t.strip() for t in args.ticker.split(',') if t.strip()]
    
    with builder.conn.cursor() as cursor:
        start_ts = datetime.utcnow()
        
        for ticker in tickers:
            print(f"Ticker: {ticker}")
            
            if overwrite_flag:
                if full_mode:
                    cursor.execute(
                        f"DELETE FROM {analytics_table} WHERE ticker = %s",
                        (ticker,)
                    )
                else:
                    cursor.execute(
                        f"DELETE FROM {analytics_table} WHERE ticker = %s AND date::date BETWEEN %s AND %s",
                        (ticker, args.start_date, args.end_date)
                    )
            
            insert_sql = f"""
                WITH base AS (
                    SELECT date, ticker, open, close, adj_close, high, low, volume
                    FROM {raw_table}
                    WHERE ticker = %s
                    AND (%s = 'full' OR date::date BETWEEN %s AND %s)
                ), 
                daily_returns AS (
                    SELECT
                        *,
                        LAG(close) OVER (PARTITION BY ticker ORDER BY date) AS prev_close,
                        CASE WHEN open > 0 THEN (close - open) / open ELSE NULL END AS return_close_open,
                        CASE WHEN LAG(close) OVER (PARTITION BY ticker ORDER BY date) > 0
                             THEN close / NULLIF(LAG(close) OVER (PARTITION BY ticker ORDER BY date), 0) - 1
                             ELSE NULL END AS return_prev_close
                    FROM base
                ),
                volatility_calc AS (
                    SELECT
                        *,
                        STDDEV_SAMP(return_prev_close) OVER (
                            PARTITION BY ticker ORDER BY date
                            ROWS BETWEEN {args.vol_window - 1} PRECEDING AND CURRENT ROW
                        ) AS volatility_n_days
                    FROM daily_returns
                ),
                final_features AS (
                    SELECT
                        date, ticker, open, close, adj_close, prev_close, high, low, volume,
                        return_close_open, return_prev_close, volatility_n_days,
                        EXTRACT(YEAR FROM date) AS year,
                        EXTRACT(MONTH FROM date) AS month,
                        EXTRACT(DOW FROM date) AS day_of_week,
                        CASE WHEN EXTRACT(DOW FROM date) = 1 THEN 1 ELSE 0 END AS is_monday,
                        CASE WHEN EXTRACT(DOW FROM date) = 5 THEN 1 ELSE 0 END AS is_friday,
                        CASE WHEN close > open THEN 1 ELSE 0 END AS target_up

                    FROM volatility_calc
                    WHERE
                        open IS NOT NULL AND close IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL 
                        AND adj_close IS NOT NULL AND volume IS NOT NULL
                        AND open > 0 AND close > 0 AND adj_close > 0 AND volume >= 0
                        AND high >= GREATEST(open, close, low)
                        AND low <= LEAST(open, close, high)
                        AND prev_close IS NOT NULL
                        AND return_close_open IS NOT NULL
                        AND return_prev_close IS NOT NULL
                        AND volatility_n_days IS NOT NULL
                )
                INSERT INTO {analytics_table} (
                    date, ticker, year, month, day_of_week,
                    open, close, adj_close, prev_close, high, low, volume,
                    return_close_open, return_prev_close, volatility_n_days,
                    is_monday, is_friday, target_up,
                    run_id, ingested_at_utc, source_name
                )
                SELECT
                    date, ticker, year, month, day_of_week,
                    open, close, adj_close, prev_close, high, low, volume,
                    return_close_open, return_prev_close, volatility_n_days,
                    is_monday, is_friday, target_up,
                    %s AS run_id,
                    now() AS ingested_at_utc,
                    'Yahoo Finance' AS source_name
                FROM final_features
                ON CONFLICT (date, ticker) DO UPDATE SET
                    year = EXCLUDED.year,
                    month = EXCLUDED.month,
                    day_of_week = EXCLUDED.day_of_week,
                    open = EXCLUDED.open,
                    close = EXCLUDED.close,
                    adj_close = EXCLUDED.adj_close,
                    prev_close = EXCLUDED.prev_close,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    volume = EXCLUDED.volume,
                    return_close_open = EXCLUDED.return_close_open,
                    return_prev_close = EXCLUDED.return_prev_close,
                    volatility_n_days = EXCLUDED.volatility_n_days,
                    is_monday = EXCLUDED.is_monday,
                    is_friday = EXCLUDED.is_friday,
                    target_up = EXCLUDED.target_up,
                    run_id = EXCLUDED.run_id,
                    ingested_at_utc = EXCLUDED.ingested_at_utc,
                    source_name = EXCLUDED.source_name;
            """
            
            cursor.execute(insert_sql, (ticker, args.mode, args.start_date, args.end_date, args.run_id))
            
            if full_mode:
                validation_sql = f"""
                    SELECT COUNT(*) FROM {analytics_table} WHERE ticker = %s AND (
                        date IS NULL OR ticker IS NULL OR open IS NULL OR close IS NULL 
                        OR high IS NULL OR low IS NULL OR adj_close IS NULL OR volume IS NULL
                        OR open <= 0 OR close <= 0 OR adj_close <= 0 OR prev_close <= 0 OR volume < 0
                        OR high < GREATEST(open, close, low)
                        OR low > LEAST(open, close, high)
                        OR return_close_open IS NULL OR return_prev_close IS NULL OR volatility_n_days IS NULL
                    )
                """
                cursor.execute(validation_sql, (ticker,))
            else:
                validation_sql = f"""
                    SELECT COUNT(*) FROM {analytics_table} 
                    WHERE ticker = %s AND date::date BETWEEN %s AND %s AND (
                        date IS NULL OR ticker IS NULL OR open IS NULL OR close IS NULL 
                        OR high IS NULL OR low IS NULL OR adj_close IS NULL OR volume IS NULL
                        OR open <= 0 OR close <= 0 OR adj_close <= 0 OR prev_close <= 0 OR volume < 0
                        OR high < GREATEST(open, close, low)
                        OR low > LEAST(open, close, high)
                        OR return_close_open IS NULL OR return_prev_close IS NULL OR volatility_n_days IS NULL
                    )
                """
                cursor.execute(validation_sql, (ticker, args.start_date, args.end_date))
            
            violations = cursor.fetchone()[0]
            if violations and violations > 0:
                builder.conn.rollback()
                raise ValueError(f"Validación fallida: {violations} filas inválidas para {ticker}")
            
            if full_mode:
                cursor.execute(
                    f"SELECT MIN(date), MAX(date), COUNT(*) FROM {analytics_table} WHERE ticker = %s",
                    (ticker,)
                )
            else:
                cursor.execute(
                    f"SELECT MIN(date), MAX(date), COUNT(*) FROM {analytics_table} WHERE ticker = %s AND date::date BETWEEN %s AND %s",
                    (ticker, args.start_date, args.end_date)
                )
            
            min_date, max_date, cnt = cursor.fetchone()
            duration_sec = (datetime.utcnow() - start_ts).total_seconds()
            
            print(f"Ticker {ticker} Log: Update/Create rows: {cnt}")
            print(f"Ticker {ticker} Log: Min: {min_date} | Max: {max_date}")
            print(f"Ticker {ticker} Log: Times: {duration_sec:.2f}s")
        
        builder.conn.commit()
    
    builder.disconnect()


if __name__ == '__main__':
    main()