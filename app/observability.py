"""OpenTelemetry setup: traces, metrics and logs exported via OTLP to the Collector."""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

RESOURCE = Resource.create({"service.name": "order-tracker"})

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

_configured = False


def configure_observability() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    insecure = OTLP_ENDPOINT.startswith("http://")

    tracer_provider = TracerProvider(resource=RESOURCE)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=insecure))
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=RESOURCE,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=insecure),
                export_interval_millis=5000,
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=RESOURCE)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=insecure)
        )
    )
    set_logger_provider(logger_provider)

    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(
        LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    )
