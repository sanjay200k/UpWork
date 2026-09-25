FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract.py prompts.py ./

# The input/output CSVs live in a mounted volume, e.g.:
#   docker run --rm -it -e GROQ_API_KEY=$GROQ_API_KEY \
#       -v /path/on/host:/data \
#       upwork-extractor /data/input.csv /data/output.csv
ENTRYPOINT ["python3", "extract.py"]
