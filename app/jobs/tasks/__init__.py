from app.jobs.tasks.example import example_task
from app.jobs.tasks.ingest_data_source import ingest_data_source
from app.jobs.tasks.start_deal_analysis import start_deal_analysis
from app.jobs.tasks.start_deal_screening import start_deal_screening
from app.jobs.tasks.start_deal_verification import start_deal_verification

# The SAQ worker only runs what is listed here. A task missing from this list
# is enqueued and then silently never consumed -- no error on either side.
functions = [
    example_task,
    ingest_data_source,
    start_deal_analysis,
    start_deal_verification,
    start_deal_screening,
]
