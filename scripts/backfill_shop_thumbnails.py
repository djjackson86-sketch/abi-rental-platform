"""Bounded, resumable thumbnails-only backfill; originals and editor choices stay intact.

Run with configured app environment: python scripts/backfill_shop_thumbnails.py
Add --apply --limit 20 --after-id 0 to write one small batch. No startup work.
"""
import argparse
import io
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
from app import create_app
from app.db import get_db, write_transaction
from app.services import shop_catalogue as shop


def run(limit=20, after_id=0, apply=False):
    db=get_db()
    rows=db.execute('SELECT i.id FROM shop_listing_images i LEFT JOIN shop_listing_thumbnails t ON t.image_id=i.id WHERE i.id>? AND t.image_id IS NULL ORDER BY i.id LIMIT ?', (after_id,min(50,max(1,limit)))).fetchall()
    result={'processed':0,'original_bytes':0,'thumbnail_bytes':0,'last_id':after_id,'apply':apply}
    for row in rows:
        mid=int(row[0])
        data,mime,version,owner=shop.image_bytes(mid)
        if data is None:continue
        with Image.open(io.BytesIO(data)) as image:
            if image.width*image.height>shop.MAX_IMAGE_PIXELS:
                raise ValueError('Stored image exceeds safe decode limit: '+str(mid))
            thumb,w,h=shop._encode_thumb(image,mime)
        if apply:
            with write_transaction():
                # Do not attach a thumbnail to a photo replaced while decoding.
                current=get_db().execute('SELECT version FROM shop_listing_images WHERE id=?',(mid,)).fetchone()
                if current is None or int(current[0])!=version:continue
                shop._save_thumbnail(mid,thumb,mime,w,h)
        result['processed']+=1
        result['original_bytes']+=len(data)
        result['thumbnail_bytes']+=len(thumb)
        result['last_id']=mid
    return result


if __name__=='__main__':
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--after-id',type=int,default=0)
    args=parser.parse_args()
    app=create_app()
    with app.app_context():print(json.dumps(run(args.limit,args.after_id,args.apply)))
