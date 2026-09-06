FROM rclone/rclone:latest
RUN apk add --no-cache python3 bash tzdata
WORKDIR /app
ENTRYPOINT ["python3", "/app/scripts/sync_manager.py"]
CMD ["--daemon"]
