FROM python:3.11-slim

WORKDIR /app

COPY quant/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

WORKDIR /app/quant

ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000 7860

CMD ["python", "api.py"]
