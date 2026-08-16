FROM python:3.12-slim

WORKDIR /app

COPY app.py /app/app.py

RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "app.py"]
