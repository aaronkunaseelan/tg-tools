"""Status totals come from controller counts, not a page of jobs."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import meeting_gateway_adapter

class StatusCoverage(unittest.TestCase):
    def test_status_must_count_pending_jobs_older_than_twenty_completions(self):
        rows = [{'status': 'succeeded', 'jobId': str(i)} for i in range(20)]
        rows.append({'status': 'pending', 'jobId': 'older-pending'})
        def list_jobs(**kwargs):
            visible = rows
            if kwargs.get('status'):
                visible = [r for r in rows if r['status'] in kwargs['status'].split(',')]
            return {'items': visible[:kwargs['limit']], 'count': len(visible)}
        adapter = meeting_gateway_adapter.GatewayAdapter(settings=NS())
        gateway = NS(send_text=AsyncMock(return_value=10))
        runtime = NS(runtime_client=NS(list_jobs=list_jobs), gateway=gateway)
        message = NS(chat_id=1, thread_id=2, message_id=3)
        asyncio.run(adapter.handle_command(runtime, message, command='status', arg=''))
        text = gateway.send_text.call_args.args[0].text
        self.assertIn('pending=1', text)


