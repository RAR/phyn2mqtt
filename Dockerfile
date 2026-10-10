FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY phyn2mqtt.py .
USER nobody
CMD ["python", "-u", "phyn2mqtt.py"]
