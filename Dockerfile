FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "requests==2.32.3"
COPY service.py .
EXPOSE 8787
CMD ["python", "-u", "service.py"]
