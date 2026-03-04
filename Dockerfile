FROM python:3.12.13-alpine

#RUN apk add --update gcc libc-dev linux-headers libffi-dev

ENV APP_HOME=/app
WORKDIR /

COPY ./app $APP_HOME
COPY *.py package.json requirements.txt /
RUN pip install -r requirements.txt

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "main.py", "-c", "/exporter_config.yaml"]