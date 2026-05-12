FROM python:3.11-slim

WORKDIR /app

# Copy only the server file from the cloned repo
COPY --chmod=755 sudoku_server.py .

RUN pip install --no-cache-dir websockets

# Run as non-root for security
RUN useradd -m sudoku
USER sudoku

EXPOSE 8765

CMD ["python", "-u", "sudoku_server.py"]
