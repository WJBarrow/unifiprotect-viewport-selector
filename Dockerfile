FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY service.py .
EXPOSE 8787
CMD ["python", "-u", "service.py"]
