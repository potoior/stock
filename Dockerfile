FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir pandas backtrader fastapi uvicorn gradio

COPY . .

EXPOSE 8000 7860

WORKDIR /app/quant
CMD ["python", "api.py"]