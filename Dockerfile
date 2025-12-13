FROM python:3.10-slim

# setear workdir
WORKDIR /app

# dependencias del sistema
RUN apt-get update

# copia requirements e instala dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r /app/requirements.txt

# copia código
COPY . .

EXPOSE 8000

# Comando por defecto para levantar la API
CMD ["uvicorn", "scripts.app:app", "--host", "0.0.0.0", "--port", "8000"]
