"""External corroboration source adapters (Epic 12).

Each module here implements the CorroborationSource protocol from
app.services.corroboration and is registered into CORROBORATION_SOURCES only
once the corroboration pass's I/O placement is settled (SIM-253), so a network
call never sits inside the verify transaction unresolved.
"""
