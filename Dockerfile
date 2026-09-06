FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/data && useradd --system --uid 10001 ozon && chown -R ozon /app
USER ozon

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

# Внутри контейнера слушаем все интерфейсы — иначе опубликованный порт
# до панели не достучится. Снаружи доступ ограничивается адресом публикации
# (BIND_ADDR в docker-compose.yml), а не этой строкой.
#
# --forwarded-allow-ips здесь намеренно не задан: uvicorn возьмёт его из
# переменной FORWARDED_ALLOW_IPS (по умолчанию 127.0.0.1). Прежнее значение
# "*" заставляло доверять заголовку X-Forwarded-For от кого угодно, а значит
# любой мог подделать свой адрес и обойти IP_ALLOWLIST и защиту от перебора.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
