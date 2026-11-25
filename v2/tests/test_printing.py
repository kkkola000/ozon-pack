"""Real PostgreSQL, exact print payloads, dispatch races and no live Ozon/printer calls."""

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import zxingcpp
from conftest import PASSWORD, login
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from instance_remote import GTIN, mark
from pypdf import PdfReader
from sqlalchemy import delete, func, select
from test_assembly import call
from test_documents import finish_labels, ready_open
from test_documents import labels as labels
from test_instance_data import KONVERT
from test_instance_data import pipeline as pipeline
from test_readiness import all_scans, complete, run_ready
from test_readiness import ready_pipeline as ready_pipeline
from test_splitting import split_pipeline as split_pipeline
from test_sticker_archive import archive as archive
from test_sticker_archive import saved, saved_mark

from pack_v2.infrastructure.models import AuditEvent
from pack_v2.modules.catalog.models import Offer, OfferCode, Product, ProductCode
from pack_v2.modules.documents.models import ShipmentLabel
from pack_v2.modules.printing import pdf, profiles
from pack_v2.modules.printing.models import PrintJob, PrintSettings
from pack_v2.modules.printing.public import Printing
from pack_v2.shared.errors import Conflict, DomainError, Forbidden, NotFound


@pytest.fixture
def printing(archive, ready_pipeline, client):
    assembly = ready_pipeline[0]
    service = Printing(
        assembly.db,
        assembly,
        assembly.documents,
        archive,
        ready_pipeline[1].marking,
        assembly.orders,
        assembly.box,
    )
    client.app.state.printing = service
    return service


def configure(printing, owner, *, product=False, marking=False, qz=False):
    value = printing.settings(owner)
    rows = deepcopy(value["profiles"])
    if qz:
        for row in rows:
            row["printer"] = "Labels" if row["kind"] == "ozon:label" else "CZ"
    return printing.save(owner, value["revision"], product, marking, rows)


def scanned_label(ready_pipeline, owner, db):
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db, code="ii50082908916"))
    return call(ready_pipeline[0], owner, view, "scan", KONVERT)["session"]


def label_job(printing, ready_pipeline, owner, db):
    view = scanned_label(ready_pipeline, owner, db)
    job = printing.plan(owner, UUID(view["id"]))["jobs"][0]
    return view, job


def test_settings_save_is_atomic_revisioned_and_does_not_expose_signing_key(printing, owner, client, db):
    value = printing.settings(owner)
    value["product_barcodes"] = value["marking_duplicates"] = True
    value["profiles"][0].update(
        size="custom", width=60, height=45, x=-2.5, y=1, gap=2, orientation="landscape"
    )
    csrf = saved(client).csrf_token
    assert client.post("/api/v2/printing/settings", json=value).status_code == 403
    response = client.post("/api/v2/printing/settings", json=value, headers={"x-csrf-token": csrf})
    assert response.status_code == 200 and response.json()["revision"] == value["revision"] + 1
    assert printing.settings(owner)["profiles"][0]["x"] == -2.5
    assert (
        client.post("/api/v2/printing/settings", json=value, headers={"x-csrf-token": csrf}).status_code
        == 409
    )
    result = printing.settings(owner)
    bad = deepcopy(result)
    bad["profiles"].append(bad["profiles"][1])
    assert (
        client.post("/api/v2/printing/settings", json=bad, headers={"x-csrf-token": csrf}).status_code == 400
    )
    assert printing.settings(owner) == result
    assert "encrypted_key" not in response.text and "certificate" not in response.text
    with db.transaction() as tx:
        assert (
            tx.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.kind == "printing_settings_saved")
            )
            == 1
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"gap": -1},
        {"x": 31},
        {"size": "bad"},
        {"printer": "a\nb"},
        {"orientation": "upside-down"},
        {"size": "custom", "width": float("nan")},
    ],
)
def test_profile_validation_rejects_unsafe_or_invalid_geometry(changes):
    with pytest.raises(DomainError):
        profiles.profile("ozon:label", **changes)


def test_automatic_label_needs_product_scan_then_resumes_same_job(printing, ready_pipeline, owner, db):
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db, code="ii50082908916"))
    # This helper opens with a label, without a physical product scan.
    assert view["scanned"] == 0
    assert printing.plan(owner, UUID(view["id"]))["jobs"] == []
    view = call(ready_pipeline[0], owner, view, "scan", KONVERT)["session"]
    first = printing.plan(owner, UUID(view["id"]))["jobs"]
    assert len(first) == 1 and first[0]["state"] == "prepared"
    assert printing.plan(owner, UUID(view["id"]))["jobs"] == first
    token, job_id = UUID(first[0]["dispatch_token"]), UUID(first[0]["id"])
    permission = printing.start(owner, job_id, token)
    assert permission["dispatch"]
    assert not printing.start(owner, job_id, token)["dispatch"]
    printing.finish(owner, job_id, UUID(permission["delivery_token"]), "submitted")
    assert printing.plan(owner, UUID(view["id"]))["jobs"][0]["state"] == "submitted"
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(PrintJob)) == 1


def test_product_pages_precede_original_and_original_archive_is_unchanged(
    printing, ready_pipeline, labels, owner, db, archive, client
):
    configure(printing, owner, product=True)
    view, job = label_job(printing, ready_pipeline, owner, db)
    remote_calls = len(ready_pipeline[2].instance_calls)
    printed = PdfReader(io.BytesIO(printing.file(owner, UUID(job["id"]))))
    assert len(printed.pages) == 4
    texts = [page.extract_text() for page in printed.pages]
    assert all(texts[:-1]) and texts[-1] == ""
    assert any("OZN" in text for text in texts[:-1])
    row = archive.listing(saved(client))["items"][0]
    assert archive.file(owner, UUID(row["id"]))[0] == labels.file
    configure(printing, owner, product=False)
    manual = printing.label(owner, uuid4(), UUID(row["id"]), row["generation"])
    assert len(PdfReader(io.BytesIO(printing.file(owner, UUID(manual["id"])))).pages) == 1
    assert ready_pipeline[0].session(owner, UUID(view["id"]))["scanned"] == 1
    assert not ready_pipeline[2].ship_calls and len(ready_pipeline[2].instance_calls) == remote_calls


def test_code_choice_prefers_own_market_then_own_then_linked_main_without_market_prefix(
    printing, ready_pipeline, owner, db
):
    catalog = ready_pipeline[0].orders.catalog
    with db.transaction() as tx:
        offer = tx.scalar(select(Offer))
        pid = uuid4()
        tx.add(Product(id=pid, name="Main"))
        tx.flush()
        offer.product_id = pid
        tx.add_all(
            [
                ProductCode(product_id=pid, kind="other", value=value, origin="manual")
                for value in ("MY-CODE", "MRKT-wrong", "OZN-wrong")
            ]
        )
        tx.execute(delete(OfferCode).where(OfferCode.offer_id == offer.id))
        tx.add_all(
            [
                OfferCode(offer_id=offer.id, kind="other", value=v, origin="manual")
                for v in ("1234567890123", "OZN-priority")
            ]
        )
        tx.flush()
        assert catalog.print_codes(tx, {offer.id}, "OZN")[offer.id] == "OZN-priority"
        tx.execute(delete(OfferCode).where(OfferCode.offer_id == offer.id, OfferCode.value == "OZN-priority"))
        assert catalog.print_codes(tx, {offer.id}, "OZN")[offer.id] == "1234567890123"
        tx.execute(delete(OfferCode).where(OfferCode.offer_id == offer.id))
        assert catalog.print_codes(tx, {offer.id}, "OZN")[offer.id] == "MY-CODE"
        tx.execute(delete(ProductCode).where(ProductCode.product_id == pid, ProductCode.value == "MY-CODE"))
        assert catalog.print_codes(tx, {offer.id}, "OZN")[offer.id] is None
        tx.add(OfferCode(offer_id=offer.id, kind="other", value="MRKT-priority", origin="manual"))
        tx.flush()
        assert catalog.print_codes(tx, {offer.id}, "MRKT")[offer.id] == "MRKT-priority"


@pytest.mark.parametrize("code", [mark(), mark().replace("\x1d", "<gs>"), f"01{GTIN}21ABCDEF123456793ABCD"])
def test_datamatrix_embedded_in_pdf_reads_back_exact_gs1_bytes(code):
    from pack_v2.modules.marking.codes import parse

    expected = parse(code).raw.encode("ascii")
    data = pdf.labels([code], 58, 40, marking=True)
    page = PdfReader(io.BytesIO(data)).pages[0]
    image = next(iter(page.images)).image
    result = zxingcpp.read_barcode(image)
    assert result and result.bytes == expected and result.content_type == zxingcpp.ContentType.GS1
    assert len(PdfReader(io.BytesIO(data)).pages) == 1


def test_duplicate_checkbox_and_single_shared_printer_without_changing_market_proof(
    printing, ready_pipeline, owner, db
):
    view = saved_mark(ready_pipeline, owner, db)
    assert all(j["kind"] != "marking:duplicate" for j in printing.plan(owner, UUID(view["id"]))["jobs"])
    configure(printing, owner, marking=True, qz=True)
    jobs = printing.plan(owner, UUID(view["id"]))["jobs"]
    marks = [j for j in jobs if j["kind"] == "marking:duplicate"]
    assert len(marks) == 1 and marks[0]["profile"]["printer"] == "CZ"
    data = printing.file(owner, UUID(marks[0]["id"]))
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
    assert ready_pipeline[0].session(owner, UUID(view["id"]))["data"]["can_verify"]
    before = len(ready_pipeline[2].instance_calls)
    permission = printing.start(owner, UUID(marks[0]["id"]), UUID(marks[0]["dispatch_token"]))
    printing.finish(owner, UUID(marks[0]["id"]), UUID(permission["delivery_token"]), "browser")
    assert len(ready_pipeline[2].instance_calls) == before


def test_uncertain_dispatch_is_not_repeated_and_manual_reprint_is_a_new_intention(
    printing, ready_pipeline, owner, db
):
    view, job = label_job(printing, ready_pipeline, owner, db)
    jid, token = UUID(job["id"]), UUID(job["dispatch_token"])
    permission = printing.start(owner, jid, token)
    assert permission["dispatch"]
    # Simulate losing the printer acknowledgement and reloading the page.
    assert not printing.start(owner, jid, token)["dispatch"]
    assert printing.plan(owner, UUID(view["id"]))["jobs"][0]["state"] == "dispatching"
    printing.finish(owner, jid, UUID(permission["delivery_token"]), "uncertain")
    assert not printing.start(owner, jid, token)["dispatch"]
    with db.transaction() as tx:
        original = tx.get(PrintJob, jid)
        label, generation = original.label_id, original.generation
    request = uuid4()
    manual = printing.label(owner, request, label, generation, session_id=UUID(view["id"]))
    assert manual["id"] != job["id"]
    assert printing.label(owner, request, label, generation, session_id=UUID(view["id"])) == manual
    with pytest.raises(Conflict):
        printing.label(owner, request, label, generation + 1, session_id=UUID(view["id"]))


def test_simultaneous_tabs_receive_one_dispatch_permission(printing, ready_pipeline, owner, db):
    view, job = label_job(printing, ready_pipeline, owner, db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: printing.start(owner, UUID(job["id"]), UUID(job["dispatch_token"])), range(2))
        )
    assert sum(r["dispatch"] for r in results) == 1
    permission = next(r for r in results if r["dispatch"])
    with pytest.raises(Forbidden):
        printing.finish(owner, UUID(job["id"]), UUID(job["dispatch_token"]), "uncertain")
    assert "delivery_token" not in printing.plan(owner, UUID(view["id"]))["jobs"][0]
    printing.finish(owner, UUID(job["id"]), UUID(permission["delivery_token"]), "submitted")


@pytest.mark.parametrize("change", ["generation", "cancelled", "expired"])
def test_stale_sources_stop_dispatch(printing, ready_pipeline, owner, db, change):
    view, job = label_job(printing, ready_pipeline, owner, db)
    if change == "cancelled":
        call(ready_pipeline[0], owner, view, "cancel")
    else:
        with db.transaction() as tx:
            if change == "generation":
                tx.scalar(select(ShipmentLabel)).generation += 1
            else:
                tx.get(PrintJob, UUID(job["id"])).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(Conflict):
        printing.start(owner, UUID(job["id"]), UUID(job["dispatch_token"]))
    with db.transaction() as tx:
        assert tx.get(PrintJob, UUID(job["id"])).started_at is None


def test_unbound_mark_cannot_be_printed_from_an_old_job(printing, archive, ready_pipeline, owner, db, client):
    saved_mark(ready_pipeline, owner, db)
    row = archive.listing(saved(client))["items"][0]
    code = archive.marking(owner, UUID(row["id"]))["marks"][0]
    job = printing.marks(owner, uuid4(), UUID(row["id"]), [{"id": code["id"], "revision": code["revision"]}])
    assert printing.file(owner, UUID(job["id"])).startswith(b"%PDF-")
    archive.release(owner, UUID(row["id"]), UUID(code["id"]), code["revision"])
    with pytest.raises(Conflict):
        printing.file(owner, UUID(job["id"]))
    with pytest.raises(Conflict):
        printing.start(owner, UUID(job["id"]), UUID(job["dispatch_token"]))


def test_owner_permissions_csrf_and_job_isolation(printing, identity, ready_pipeline, owner, db, client):
    _, job = label_job(printing, ready_pipeline, owner, db)
    path = f"/api/v2/printing/jobs/{job['id']}"
    assert client.post(path + "/start", json={"dispatch_token": job["dispatch_token"]}).status_code == 403
    user = identity.create_user(owner, "other-printer", PASSWORD, "packer", frozenset({"stickers"}))
    with pytest.raises(NotFound):
        printing.file(user, UUID(job["id"]))
    csrf = saved(client).csrf_token
    client.post("/logout", data={"csrf": csrf})
    login(client, "other-printer")
    assert client.get(path + "/pdf").status_code == 404
    assert client.get("/settings/printing").status_code == 200
    assert client.get("/settings").status_code == 403


def test_qz_key_is_encrypted_and_only_authenticated_csrf_protected_hashes_are_signed(
    printing, owner, db, client
):
    cert = printing.certificate(owner)
    assert printing.certificate(owner) == cert
    value = "a" * 64
    csrf = saved(client).csrf_token
    assert client.post("/api/v2/printing/qz/sign", content=value).status_code == 403
    invalid = client.post(
        "/api/v2/printing/qz/sign", content="print arbitrary content", headers={"x-csrf-token": csrf}
    )
    assert invalid.status_code == 400
    response = client.post("/api/v2/printing/qz/sign", content=value, headers={"x-csrf-token": csrf})
    assert response.status_code == 200
    public = x509.load_pem_x509_certificate(cert.encode()).public_key()
    public.verify(base64.b64decode(response.text), value.encode(), padding.PKCS1v15(), hashes.SHA512())
    with db.transaction() as tx:
        row = tx.get(PrintSettings, 1)
        assert b"PRIVATE KEY" not in row.encrypted_key
        assert b"PRIVATE KEY" in printing.box.open(row.encrypted_key)
        assert mark() not in repr(list(tx.scalars(select(AuditEvent.details))))


def test_paper_orientation_gap_and_offsets_are_applied_to_transient_copy():
    row = profiles.profile(
        "ozon:label", size="custom", width=60, height=40, orientation="portrait", gap=2, x=1, y=-1
    )
    result = pdf.fit(pdf.sample(row), row)
    width, height = pdf.measure(result)
    assert width == pytest.approx(40, abs=0.01) and height == pytest.approx(62, abs=0.01)


@pytest.mark.parametrize("keep_root", [False, True])
def test_split_prints_each_child_only_and_does_not_repeat_relocated_mark(
    printing, split_pipeline, owner, db, keep_root
):
    configure(printing, owner, product=True, marking=True, qz=True)
    split_pipeline[2].keep_root = keep_root
    view = all_scans(split_pipeline, owner)
    before = printing.plan(owner, UUID(view["id"]))["jobs"]
    assert len(before) == 1 and before[0]["kind"] == "marking:duplicate"
    job = before[0]
    permission = printing.start(owner, UUID(job["id"]), UUID(job["dispatch_token"]))
    printing.finish(owner, UUID(job["id"]), UUID(permission["delivery_token"]), "submitted")
    view = complete(split_pipeline, owner, view)
    run_ready(split_pipeline)
    run_ready(split_pipeline)
    current = finish_labels(split_pipeline, owner, view)
    assert current["status"] == "ready"
    jobs = printing.plan(owner, UUID(view["id"]))["jobs"]
    labels = [j for j in jobs if j["kind"] == "ozon:label"]
    marks = [j for j in jobs if j["kind"] == "marking:duplicate"]
    assert len(labels) == 3 and len(marks) == 1 and marks[0]["id"] == job["id"]
    for label in labels:
        data = PdfReader(io.BytesIO(printing.file(owner, UUID(label["id"]))))
        assert len(data.pages) == 2 and data.pages[0].extract_text() and not data.pages[1].extract_text()


def test_enabled_barcode_prepend_skips_products_without_any_code(printing, ready_pipeline, owner, db):
    configure(printing, owner, product=True)
    view = scanned_label(ready_pipeline, owner, db)
    with db.transaction() as tx:
        tx.execute(delete(OfferCode))
        tx.execute(delete(ProductCode))
    job = printing.plan(owner, UUID(view["id"]))["jobs"][0]
    assert len(PdfReader(io.BytesIO(printing.file(owner, UUID(job["id"])))).pages) == 1
