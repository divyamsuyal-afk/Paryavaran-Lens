THE LAMBDA FUNCTION USED VIA THE AWS CONSOLE-
import base64
import json
import time
import uuid
from decimal import Decimal

import boto3
from botocore.config import Config

# ---- Your AWS resources (already created) ----
REGION = "ap-southeast-2"
TABLE_NAME = "paryavaran-reports"
BUCKET = "paryavaran-lens-photos-48213"

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
rekognition = boto3.client("rekognition", region_name=REGION)

CATS = {
    "garbage_pile": {"title": "Garbage pile", "authority": "MCD"},
    "burning_waste": {"title": "Burning waste", "authority": "DPCC"},
    "river_foam": {"title": "River foam / water pollution", "authority": "DPCC / DJB"},
    "blocked_drain": {"title": "Blocked drain", "authority": "DJB"},
}
STATUSES = {"reported", "forwarded", "in_progress", "fixed"}

# Words that Amazon Rekognition labels are matched against (checked in this order).
KEYWORDS = [
    ("burning_waste", ["fire", "flame", "bonfire", "smoke", "burning"]),
    ("river_foam", ["foam", "suds", "froth"]),
    ("blocked_drain", ["drain", "sewer", "manhole", "gutter", "sewage"]),
    ("garbage_pile", ["garbage", "trash", "litter", "rubbish", "waste", "junk", "dump", "landfill"]),
]


def detect_category(img_bytes):
    """Ask Rekognition what is in the photo and map it to one of our problem types.
    Returns (category or None, list of label names seen)."""
    res = rekognition.detect_labels(Image={"Bytes": img_bytes}, MaxLabels=25, MinConfidence=55)
    names = [l["Name"] for l in res.get("Labels", [])]
    lowered = [n.lower() for n in names]
    for category, words in KEYWORDS:
        if any(w in n for n in lowered for w in words):
            return category, names
    return None, names
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _num(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Cannot serialise {type(o)}")


def resp(code, body):
    # CORS headers are added by API Gateway (CORS menu), so not repeated here.
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_num),
    }


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Body must be valid JSON")


def presign(key):
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=3600,
    )


def public_view(item):
    out = {k: v for k, v in item.items() if k != "photo_key"}
    out["photo_url"] = presign(item["photo_key"]) if item.get("photo_key") else ""
    return out


def list_reports():
    items = []
    kwargs = {}
    while True:
        r = table.scan(**kwargs)
        items.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r:
            break
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return resp(200, [public_view(i) for i in items])


def create_report(body):
    img_b64 = body.get("image_base64")
    if not img_b64:
        raise ValueError("image_base64 is required")
    try:
        img = base64.b64decode(img_b64)
    except Exception:
        raise ValueError("image_base64 is not valid base64")
    if len(img) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (max 5 MB)")

    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
    except (TypeError, ValueError):
        raise ValueError("lat and lng must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("lat/lng out of range")

    # If the person picked a type in the dropdown, use it. Otherwise let AI decide.
    category = body.get("category_override")
    labels = []
    if category not in CATS:
        category, labels = detect_category(img)
        if category is None:
            seen = ", ".join(labels[:6]) or "nothing clear"
            raise ValueError(
                f"Could not tell the problem type from this photo (AI saw: {seen}). "
                "Please choose the problem type from the dropdown."
            )

    report_id = uuid.uuid4().hex[:10]
    photo_key = f"photos/{report_id}.jpg"
    s3.put_object(Bucket=BUCKET, Key=photo_key, Body=img, ContentType="image/jpeg")

    item = {
        "id": report_id,
        "lat": Decimal(str(lat)),
        "lng": Decimal(str(lng)),
        "category": category,
        "title": CATS[category]["title"],
        "authority": CATS[category]["authority"],
        "status": "forwarded",
        "created": int(time.time()),
        "note": str(body.get("note") or "")[:300],
        "photo_key": photo_key,
        "ai_labels": labels[:8],
    }
    table.put_item(Item=item)
    return resp(200, public_view(item))


def set_status(report_id, body):
    status = body.get("status")
    if status not in STATUSES:
        raise ValueError("Invalid status")
    try:
        r = table.update_item(
            Key={"id": report_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
            ConditionExpression="attribute_exists(id)",
            ReturnValues="ALL_NEW",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return resp(404, {"error": "Report not found"})
    return resp(200, public_view(r["Attributes"]))


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    try:
        if route == "GET /reports":
            return list_reports()
        if route == "POST /report":
            return create_report(parse_body(event))
        if route == "POST /report/{id}/status":
            return set_status(event["pathParameters"]["id"], parse_body(event))
        return resp(404, {"error": "Route not found"})
    except ValueError as e:
        return resp(400, {"error": str(e)})
    except Exception as e:
        print("ERROR:", repr(e))
        return resp(500, {"error": "Server error"})
