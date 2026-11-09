FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530 AS build

WORKDIR /app
COPY experimental/ ./experimental/
COPY prototype/ ./prototype/
COPY skills/ ./skills/
COPY vendor/ ./vendor/
COPY model_registry.json INKSTONE_ROUTES.json ROUTE_GUARDS.json ./
# TripPlan demo endpoint and UI require the canonical, public MOCK fixture at runtime.
COPY docs/contracts/trip_plan_v0.json ./docs/contracts/trip_plan_v0.json
COPY research/working_snapshots/stage6/public_hotel_probe.json research/working_snapshots/stage6/public_detail_probe.json ./research/working_snapshots/stage6/
RUN NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    python -B -S -X utf8 experimental/full_server.py --smoke --storage /tmp/traveltech-smoke

FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRAVELTECH_MVP_DETERMINISTIC=1 \
    NO_PROXY=localhost,127.0.0.1 \
    no_proxy=localhost,127.0.0.1
WORKDIR /app
RUN groupadd --gid 10001 traveltech \
    && useradd --uid 10001 --gid 10001 --create-home traveltech \
    && mkdir -p /app/runtime \
    && chown traveltech:traveltech /app/runtime
COPY --from=build --chown=traveltech:traveltech /app /app
USER traveltech
EXPOSE 8080
CMD ["python", "-X", "utf8", "experimental/full_server.py", "--host", "0.0.0.0", "--port", "8080", "--storage", "/app/runtime"]
