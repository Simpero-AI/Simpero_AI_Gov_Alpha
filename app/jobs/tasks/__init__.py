from app.jobs.tasks.example import example_task
from app.jobs.tasks.ingest_data_source import ingest_data_source
from app.jobs.tasks.start_deal_analysis import start_deal_analysis
from app.jobs.tasks.start_deal_verification import start_deal_verification

functions = [example_task, ingest_data_source, start_deal_analysis, start_deal_verification]
