FROM node:20-slim AS ui-builder
WORKDIR /ui
RUN npm install -g pnpm
COPY ui/package.json ./
RUN pnpm install
COPY ui/ .
RUN pnpm build

FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ ./api/
COPY --from=ui-builder /ui/dist ./static
EXPOSE 8890
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8890"]
