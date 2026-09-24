FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY assay ./assay
RUN pip install --no-cache-dir ".[postgres]"
ENV ASSAY_STORE_URL=sqlite:////data/assay.db
VOLUME /data
EXPOSE 8400
CMD ["assay", "serve", "--host", "0.0.0.0", "--port", "8400"]
