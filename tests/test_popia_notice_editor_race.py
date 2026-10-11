"""Force the change BETWEEN the read and INSERT, rather than testing only a stale form."""
import hashlib
import pytest
from app import create_app
from app.db import get_db
from app.services import popia_notice_editor as editor
from app.services.timezone import local_now_iso


def test_atomic_insert_guard_survives_concurrent_publication(tmp_path, monkeypatch):
    app = create_app({'TESTING':True,'DATABASE':str(tmp_path/'race.sqlite'),
                      'TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','SECRET_KEY':'race-test',
                      'ADMIN_PASSWORD':'synthetic-race-password','TELEGRAM_NOTIFICATIONS_ENABLED':''})
    with app.app_context():
        db = get_db()
        db.execute('INSERT INTO popia_notice_versions (notice_version,notice_content_hash,notice_text,published_at) VALUES (?,?,?,?)',
                   ('initial',hashlib.sha256(b'Original notice').hexdigest(),'Original notice','2026-09-23T09:00:00+02:00'))
        db.commit()
        def publish_competing_version():
            db.execute('INSERT INTO popia_notice_versions (notice_version,notice_content_hash,notice_text,published_at) VALUES (?,?,?,?)',
                       ('race-winner',hashlib.sha256(b'Competing notice').hexdigest(),'Competing notice',local_now_iso()))
            db.commit()
            return 'race-loser'
        monkeypatch.setattr(editor,'_new_version',publish_competing_version)
        with pytest.raises(editor.NoticeConflict):
            editor.publish_notice('Stale replacement','initial')
        assert editor.current_notice()['notice_version']=='race-winner'
        assert db.execute('SELECT COUNT(*) FROM popia_notice_versions').fetchone()[0]==2
        assert db.execute('SELECT id FROM popia_notice_versions WHERE notice_version=?',('race-loser',)).fetchone() is None
