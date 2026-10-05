import asyncio
import json

import httpx
import pytest

from backend import config
from backend.llm import ModelClient, ModelError
from backend.schemas import TrialReportAnalysis


@pytest.mark.parametrize('phase,budget',[('trial_report',8192),('classroom',3500)])
def test_report_has_its_own_output_budget(monkeypatch,phase,budget):
    monkeypatch.setattr(config,'API_KEY','test-key')
    requests=[]
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
    async def run():
        async def emit(*args):pass
        client=ModelClient(emit)
        await client.http.aclose()
        client.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:await client.generate(phase,'test',{},TrialReportAnalysis)
        finally:await client.close()
    asyncio.run(run())
    assert requests[0]['max_tokens']==budget


def test_truncated_report_has_specific_error_and_no_identical_retry(monkeypatch):
    monkeypatch.setattr(config,'API_KEY','test-key')
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{"taught_points":['}}]})
    async def run():
        async def emit(*args):pass
        client=ModelClient(emit)
        await client.http.aclose()
        client.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:await client.generate('trial_report','test',{},TrialReportAnalysis)
        finally:await client.close()
    with pytest.raises(ModelError,match='报告.*截断'):asyncio.run(run())
    assert len(calls)==1
