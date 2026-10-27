FROM python:3.12-slim

WORKDIR /srv/mymem

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

ENV MYMEM_DATA_DIR=/data
EXPOSE 8700

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8700"]
