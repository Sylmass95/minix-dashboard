FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir flask docker requests
COPY app.py .
COPY templates/ templates/
EXPOSE 9090
CMD ["python", "app.py"]
