FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD exec gunicorn --bind :80 --workers 1 --threads 8 --timeout 0 app:app