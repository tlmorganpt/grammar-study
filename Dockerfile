FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY anki_reader.py .
COPY custom_exercises.json .
COPY Anki/ ./Anki/
RUN mkdir -p /data
ENV DATA_DIR=/data
EXPOSE 8080
CMD ["python", "anki_reader.py"]
