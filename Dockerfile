FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chmod=644 requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY --chmod=644 proxy.py wire.py tx_config.py ./
ENTRYPOINT ["python", "/app/proxy.py"]
CMD ["--help"]
