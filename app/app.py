from flask import Flask
import time
import random

from opentelemetry import trace
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


app = Flask(__name__)

# ------------------------------------------------------------
# OpenTelemetry setup
# ------------------------------------------------------------

provider = TracerProvider()

exporter = OTLPSpanExporter(
    endpoint="http://otel-agent:4318/v1/traces"
)

provider.add_span_processor(
    BatchSpanProcessor(exporter)
)

trace.set_tracer_provider(provider)

FlaskInstrumentor().instrument_app(app)


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

@app.route("/")
def home():
    return "Hello from test app!"


@app.route("/test")
def test():
    # Get the current Flask request span
    span = trace.get_current_span()

    # --------------------------------------------------------
    # Existing attributes for trace transformation testing
    # --------------------------------------------------------

    span.set_attribute(
        "custom.message",
        "before_transformation"
    )

    span.set_attribute(
        "custom.test_id",
        "trace-transform-test-001"
    )

    span.set_attribute(
        "custom.transformation_status",
        "not_transformed"
    )

    # --------------------------------------------------------
    # Numeric attribute for signal_to_metrics conversion
    # --------------------------------------------------------

    # Simulate a numeric measurement for this request.
    # The Collector will extract this value into a metric.
    work_units = random.randint(10, 100)

    span.set_attribute(
        "app.work_units",
        float(work_units)
    )

    # Add a timestamped event to the span
    span.add_event(
        "Custom trace message",
        attributes={
            "message": "Testing OTel Collector transformation",
            "stage": "before_transformation"
        }
    )

    # Simulate request processing time
    time.sleep(random.uniform(0.1, 1.0))

    return {
        "message": "test request",
        "status": "ok",
        "work_units": work_units
    }


@app.route("/error")
def error():
    raise Exception("Test error")


# ------------------------------------------------------------
# Start Flask
# ------------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080
    )

