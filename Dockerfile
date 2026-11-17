FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY --chown=app:app app ./app
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini docker-entrypoint.sh ./
USER app
ENV DATABASE_URL=sqlite:////data/adrobot.db DEMO_DATABASE_URL=sqlite:////data/keitaro-demo.db
EXPOSE 8000
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
