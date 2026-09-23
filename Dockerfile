FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tgtrader ./tgtrader
CMD ["python", "-m", "tgtrader.main"]
