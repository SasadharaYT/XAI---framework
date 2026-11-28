"""Isolated real API process for frontend/backend contract tests."""
import os
import hashlib
import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[3] / 'XAI-Backend-main' / 'XAI-Backend-main'
sys.path.insert(0, str(backend))

from app import main
import uvicorn

main.Base.metadata.create_all(main.engine)
with main.SessionLocal() as db:
    db.add(main.User(id='integration-member', email='integration@example.test', name='Integration',
                     password_hash=main.hash_password('IntegrationPass123')))
    db.add(main.TeamMember(id='integration-member', email='integration@example.test', name='Integration',
                           initials='I', role='Member', status='Active', joined_date='Today', project_ids='integration'))
    for project_id in ['integration', 'other']:
        db.add(main.Project(id=project_id, name=project_id, description='', type='test', risk='Low', last_activity='', accent=''))
    db.add(main.Agent(id='integration-agent', name='Integration agent', project_id='integration',
                      description='', framework='', endpoint=''))
    db.add(main.ProjectAPIKey(id='integration-key', project_id='integration', name='Test key',
                             key_prefix='integration', created_by='integration-member',
                             key_hash=hashlib.sha256(b'integration-sdk-key').hexdigest()))
    db.commit()

server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1',
                                     port=int(os.environ['POLICY_TEST_PORT']), log_level='error'))


@main.app.post('/__test_shutdown')
def shutdown():
    server.should_exit = True
    return {'stopping': True}


try:
    server.run()
finally:
    main.engine.dispose()
