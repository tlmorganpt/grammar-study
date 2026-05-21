FROM python:3.11-slim
WORKDIR /app
COPY anki_reader.py .
COPY requirements.txt .
COPY custom_exercises.json .
COPY Anki/ ./Anki/
RUN mkdir -p /data
ENV DATA_DIR=/data
EXPOSE 8080
CMD ["python", "anki_reader.py"]
