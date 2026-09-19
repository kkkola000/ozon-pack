"""Обновление базы прошлой версии до схемы с кабинетами.

На сервере уже лежит база без account_id: собранные отправления, товары,
возвраты и журнал. Обновление панели не должно ничего из этого потерять.
"""
import sqlite3
import subprocess

import pytest

from app import accounts, db, ozon
from app.config import BASE_DIR, settings

# Схему прошлой версии берём из истории git, а не переписываем руками:
# так тест проверяет реальную базу пользователя, а не наше представление о ней.
LEGACY_COMMIT = "0410a34"


def legacy_schema() -> str:
    try:
        source = subprocess.run(
            ["git", "-C", str(BASE_DIR), "show", f"{LEGACY_COMMIT}:app/db.py"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        # В распакованном архиве истории нет — проверять нечего, но и падать незачем.
        pytest.skip(f"нет истории git для схемы {LEGACY_COMMIT}: {exc}")
    start = source.index('SCHEMA = """') + len('SCHEMA = """')
    end = source.index('"""', start)
    return source[start:end]


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """База предыдущей версии с данными, ещё без кабинетов."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema())
    conn.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES('admin', 'x', 'admin', 1, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO postings(posting_number, order_number, status, local_state, packed_by, packed_at,"
        " items_count, positions_count, first_seen_at, updated_at)"
        " VALUES('111-222-1', '111-222', 'awaiting_deliver', 'packed', 'ivanov', '2026-01-02', 2, 1,"
        " '2026-01-01', '2026-01-02')"
    )
    conn.execute(
        "INSERT INTO posting_items(posting_number, sku, offer_id, name, quantity)"
        " VALUES('111-222-1', '555', 'ART-1', 'Кофе', 2)"
    )
    conn.execute(
        "INSERT INTO products(sku, offer_id, name, barcodes, updated_at)"
        " VALUES('555', 'ART-1', 'Кофе', '[\"4600000000017\"]', '2026-01-01')"
    )
    conn.execute("INSERT INTO product_barcodes(barcode, sku) VALUES('4600000000017', '555')")
    conn.execute(
        "INSERT INTO returns(id, type, status_sys, product_name, quantity, is_ready, first_seen_at, updated_at)"
        " VALUES('r-1', 'FBS', 'ArrivedAtReturnPlace', 'Чайник', 1, 1, '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO events(at, kind, level, posting_number, message) "
        "VALUES('2026-01-02', 'pack_complete', 'info', '111-222-1', 'Отправление собрано')"
    )
    conn.execute("INSERT INTO pack_state(user_id, posting_number, scanned) VALUES(1, '111-222-1', '{}')")
    conn.execute("INSERT INTO kv(key, value) VALUES('ozon_client_id', '123456')")
    conn.execute("INSERT INTO kv(key, value) VALUES('ozon_api_key', 'secret-key')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "db_path", str(path))
    db._local.conn = None
    ozon.reset_client()
    db.init_db()
    yield path
    existing = getattr(db._local, "conn", None)
    if existing:
        existing.close()
    db._local.conn = None


def test_data_survives_upgrade(legacy_db):
    posting = db.query_one("SELECT * FROM postings WHERE posting_number = '111-222-1'")
    assert posting is not None
    assert posting["local_state"] == "packed", "отметка сборки должна сохраниться"
    assert posting["packed_by"] == "ivanov"

    assert db.query_one("SELECT COUNT(*) AS c FROM posting_items")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM products")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM product_barcodes")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM returns")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM users")["c"] == 1


def test_everything_lands_in_one_cabinet(legacy_db):
    account = accounts.default_account()
    assert account is not None
    assert account["marketplace"] == "ozon"
    for table in ("postings", "posting_items", "products", "product_barcodes", "returns"):
        rows = db.query(f"SELECT DISTINCT account_id FROM {table}")
        assert [row["account_id"] for row in rows] == [account["id"]], table


def test_keys_move_from_kv_into_cabinet(legacy_db):
    """Ключи, введённые в прошлой версии, продолжают работать после обновления."""
    account = accounts.default_account()
    assert accounts.credentials(account) == ("123456", "secret-key", "panel")
    assert accounts.is_configured(account)


def test_old_pack_state_belongs_to_cabinet(legacy_db):
    row = db.query_one("SELECT * FROM pack_state WHERE user_id = 1")
    assert row["account_id"] == accounts.default_account()["id"]


def test_second_upgrade_is_a_no_op(legacy_db):
    """Повторный запуск панели не должен ничего ломать или дублировать."""
    before = accounts.all_accounts()
    db.init_db()
    assert len(accounts.all_accounts()) == len(before)
    assert db.query_one("SELECT COUNT(*) AS c FROM postings")["c"] == 1


def test_legacy_tables_are_gone(legacy_db):
    left = db.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%_old'"
    )
    assert not left, f"остались временные таблицы миграции: {[r['name'] for r in left]}"


def test_new_columns_are_added_to_existing_tables(legacy_db):
    """Колонки, появившиеся позже таблицы, дописываются при обновлении."""
    conn = db.connect()
    # Индексы по колонкам мешают их удалить — имитируем базу до их появления.
    conn.execute("DROP INDEX IF EXISTS idx_avito_return")
    conn.execute("ALTER TABLE avito_orders DROP COLUMN return_status")
    conn.execute("ALTER TABLE avito_orders DROP COLUMN return_tracking")
    assert "return_status" not in db._columns(conn, "avito_orders")

    db.init_db()
    columns = db._columns(conn, "avito_orders")
    assert "return_status" in columns and "return_tracking" in columns


def test_generated_data_is_purged_from_cabinets_without_keys(legacy_db):
    """Записи, оставшиеся от прежнего демо-режима, удаляются при обновлении."""
    account = accounts.default_account()
    assert db.query_one("SELECT COUNT(*) AS c FROM postings")["c"] == 1

    # Кабинет без ключей: всё, что в нём лежит, могло быть только сгенерировано.
    accounts.update(account["id"], client_id="", api_key="")
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_GENERATED_CLEANED,))
    db.init_db()

    for table in ("postings", "posting_items", "products", "product_barcodes", "returns"):
        assert db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"] == 0, table


def test_purge_runs_only_once(legacy_db):
    """Снятые ключи не должны стирать рабочую историю при каждом запуске."""
    account = accounts.default_account()
    accounts.update(account["id"], client_id="", api_key="")
    db.init_db()  # первая чистка уже прошла в фикстуре — эта ничего не трогает

    db.execute(
        "INSERT INTO postings(account_id, posting_number, status, local_state, first_seen_at, updated_at)"
        " VALUES(?, '999-1-1', 'awaiting_deliver', 'packed', '2026-01-01', '2026-01-01')",
        (account["id"],),
    )
    db.init_db()
    assert db.query_one("SELECT COUNT(*) AS c FROM postings WHERE posting_number = '999-1-1'")["c"] == 1


def test_schema_parser_ignores_sql_comments():
    """Запятая внутри комментария не должна превращаться в колонку.

    На этом уже спотыкались: комментарий в теле CREATE TABLE разрывался по
    запятой, и в ALTER TABLE уезжал кусок русского текста вместо имени колонки.
    """
    tables = [line.split()[-2] for line in db.SCHEMA.splitlines()
              if line.startswith("CREATE TABLE IF NOT EXISTS")]
    assert len(tables) >= 10, "не нашлись таблицы схемы"
    for table in tables:
        for name, definition in db._schema_columns(table):
            assert name.isascii(), f"{table}: имя колонки «{name}» не похоже на имя"
            assert name.replace("_", "").isalnum(), f"{table}: странное имя колонки «{name}»"
            assert "--" not in definition, f"{table}.{name}: в определение попал комментарий"


def test_avito_packing_columns_appear_in_old_database(tmp_path, monkeypatch):
    """Колонки сборки Avito добавляются к таблице, созданной прежней версией."""
    conn = db.connect()
    conn.execute("DROP TABLE IF EXISTS avito_orders")
    conn.execute(
        "CREATE TABLE avito_orders (account_id INTEGER NOT NULL, id TEXT NOT NULL,"
        " marketplace_id TEXT, status TEXT, local_state TEXT NOT NULL DEFAULT 'new',"
        " PRIMARY KEY (account_id, id))"
    )
    db.init_db()
    columns = set(db._columns(db.connect(), "avito_orders"))
    for name in ("packed_at", "packed_by", "claim_user_id", "claim_login", "claim_at"):
        assert name in columns, name


def test_act_columns_appear_in_old_database():
    """Колонки акта дописываются к таблице, созданной прежней версией."""
    conn = db.connect()
    conn.execute("DROP TABLE IF EXISTS return_acts")
    conn.execute(
        "CREATE TABLE return_acts (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,"
        " created_by TEXT, kind TEXT NOT NULL DEFAULT 'account', account_id INTEGER,"
        " confirmed_at TEXT, confirmed_by TEXT)"
    )
    db.init_db()
    columns = set(db._columns(db.connect(), "return_acts"))
    for name in ("received_day", "day_seq", "giveout_id", "giveout_status"):
        assert name in columns, name


def test_receipt_columns_appear_in_old_database():
    """Возвраты прежней версии получают колонки получения.

    Без них акт не собрать: раздел «Ждёт подтверждения» падал бы на первом же
    запросе к received_at, а на сервере таблица возвратов давно создана.
    """
    conn = db.connect()
    conn.execute("DROP TABLE IF EXISTS returns")
    conn.execute(
        "CREATE TABLE returns (account_id INTEGER NOT NULL, id TEXT NOT NULL,"
        " status_sys TEXT, is_ready INTEGER NOT NULL DEFAULT 0, act_id TEXT,"
        " PRIMARY KEY (account_id, id))"
    )
    db.init_db()
    columns = set(db._columns(db.connect(), "returns"))
    for name in ("received_at", "received_day", "mark", "note"):
        assert name in columns, name


def test_every_schema_table_gets_new_columns():
    """Таблицу из схемы нельзя забыть в списке миграции.

    CREATE TABLE IF NOT EXISTS не добавляет колонку в уже существующую таблицу.
    Забыли таблицу в списке — на сервере, где она создана прежней версией,
    обновление падает на первом же запросе к новой колонке. Так и случилось с
    return_acts.
    """
    import re

    in_schema = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SCHEMA))
    # Эти создаёт и наполняет сама схема при первом запуске, колонок в них не
    # прибавлялось ни разу; остальные обязаны быть в списке.
    migrated = set(db.TABLES_WITH_NEW_COLUMNS)
    forgotten = in_schema - migrated
    assert not forgotten, (
        "таблицы из схемы не попали в миграцию колонок: " + ", ".join(sorted(forgotten))
    )


def test_create_sql_survives_semicolon_in_comment():
    """Комментарий с «;» не должен обрывать оператор CREATE TABLE."""
    assert ";" in db.SCHEMA
    for line in db.SCHEMA.splitlines():
        if line.startswith("CREATE TABLE IF NOT EXISTS"):
            table = line.split()[-2]
            sql = db.create_sql(table)
            assert sql.count("(") == sql.count(")"), f"{table}: оператор оборван"
            assert sql.rstrip().endswith(");"), f"{table}: оператор оборван"


def test_automatic_acts_of_1_17_are_removed():
    """Акт, который версия 1.17 собрала сама, при обновлении убирается.

    В 1.17 акт собирался при обновлении по статусу «Получен», а вместе с этим
    статусом Ozon отдаёт весь архив — на живом складе вышел один акт на 2446
    позиций. Такой акт не подтвердить, и он закрывает собой настоящие.
    """
    from app import accounts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_AUTO_ACTS_CLEANED,))
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("auto1", db.now_iso(), "received", account_id),
    )
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, act_id, received_at, received_day) "
        "VALUES(?,?,?,?,?,?)",
        (account_id, "R-archive", 0, "auto1", db.now_iso(), db.now_iso()[:10]),
    )

    db.init_db()

    assert db.query_one("SELECT id FROM return_acts WHERE id = 'auto1'") is None
    row = db.query_one("SELECT act_id, received_at, received_day FROM returns WHERE id = 'R-archive'")
    assert row["act_id"] is None, "возврат не вернулся в работу"
    # Момент получения сброшен: следующее обновление возьмёт настоящий у Ozon,
    # и возврат встанет на своё число, а не на день обновления.
    assert row["received_at"] is None and row["received_day"] is None


def test_cleanup_keeps_marked_returns_and_runs_once():
    """Работу сборщика чистка не трогает и второй раз не запускается."""
    from app import accounts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_AUTO_ACTS_CLEANED,))
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("auto2", db.now_iso(), "received", account_id),
    )
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, act_id, mark, note) VALUES(?,?,?,?,?,?)",
        (account_id, "R-marked", 0, "auto2", "bad", "вскрыта упаковка"),
    )
    db.init_db()

    row = db.query_one("SELECT act_id, note FROM returns WHERE id = 'R-marked'")
    assert row["act_id"] == "auto2", "строка с отметкой вырвана из акта"
    assert row["note"] == "вскрыта упаковка"
    assert db.query_one("SELECT id FROM return_acts WHERE id = 'auto2'"), "акт с работой удалён"

    # Второй запуск ничего не делает: отметка о чистке уже стоит.
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("auto3", db.now_iso(), "received", account_id),
    )
    db.init_db()
    assert db.query_one("SELECT id FROM return_acts WHERE id = 'auto3'"), "чистка сработала второй раз"


def test_received_days_are_recomputed_from_the_handover():
    """Застрявший возврат встаёт на число выдачи продавцу, а не смены статуса.

    Число считалось из `visual.change_moment` — это последняя смена статуса,
    и она бывает уже в других сутках. Возвраты одной поездки расходились по
    числам: шесть в акт попадали, седьмой нет. Обновление пересчитывает число
    по `final_moment` у всего, что ещё ждёт акта.
    """
    from app import accounts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_DAYS_FIXED,))
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, final_moment, received_at, received_day) "
        "VALUES(?,?,?,?,?,?)",
        (account_id, "R-late", 0, "2026-05-10T18:50:00+00:00",
         "2026-05-11T02:10:00+00:00", "2026-05-11"),
    )
    # Нечитаемый момент: такого числа нет ни в одном календаре.
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, final_moment, received_at, received_day) "
        "VALUES(?,?,?,?,?,?)",
        (account_id, "R-junk", 0, "позавчера", "позавчера", "позавчера"),
    )

    db.init_db()

    from app import store

    late = db.query_one("SELECT received_at, received_day FROM returns WHERE id = 'R-late'")
    assert late["received_day"] == store.local_day("2026-05-10T18:50:00+00:00")
    assert late["received_at"] == "2026-05-10T18:50:00+00:00"

    # Разобрать нечего — снимаем обе отметки, следующее обновление проставит их.
    junk = db.query_one("SELECT received_at, received_day FROM returns WHERE id = 'R-junk'")
    assert junk["received_at"] is None and junk["received_day"] is None


def test_received_days_repair_does_not_touch_acts_and_runs_once():
    """Строку из акта не переносим: там работа идёт, и число менять нельзя."""
    from app import accounts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_DAYS_FIXED,))
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("act-keep", db.now_iso(), "byday", account_id),
    )
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, act_id, final_moment, received_at, received_day) "
        "VALUES(?,?,?,?,?,?,?)",
        (account_id, "R-in-act", 0, "act-keep", "2026-05-10T18:50:00+00:00",
         "2026-05-11T02:10:00+00:00", "2026-05-11"),
    )
    db.init_db()
    row = db.query_one("SELECT received_day, act_id FROM returns WHERE id = 'R-in-act'")
    assert row["received_day"] == "2026-05-11" and row["act_id"] == "act-keep"

    # Второй запуск ничего не пересчитывает: отметка о починке уже стоит.
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, final_moment, received_at, received_day) "
        "VALUES(?,?,?,?,?,?)",
        (account_id, "R-later", 0, "2026-05-10T18:50:00+00:00",
         "2026-05-11T02:10:00+00:00", "2026-05-11"),
    )
    db.init_db()
    assert db.query_one("SELECT received_day FROM returns WHERE id = 'R-later'")["received_day"] == "2026-05-11"


def test_status_change_is_filled_from_the_saved_answer():
    """Момент смены статуса достаётся из raw, а акт «без статуса» получает число.

    Колонка появилась позже, а данные для неё уже лежали в сохранённом ответе
    площадки. Без числа заголовок такого акта показывал время составления:
    «Возвраты за 16.09 11:42» читалось как «получены 16.09 в 11:42».
    """
    import json

    from app import accounts, return_acts, store

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_CHANGED_FILLED,))
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("spare1", "2026-09-16T08:42:00+00:00", "nosheet", account_id),
    )
    raw = {"visual": {"status": {"sys_name": "ArrivedAtReturnPlace"},
                      "change_moment": "2026-09-14T06:00:00.123456Z"}}
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, status_sys, act_id, raw) VALUES(?,?,?,?,?,?)",
        (account_id, "R-old", 0, "ArrivedAtReturnPlace", "spare1", json.dumps(raw)),
    )

    db.init_db()

    row = db.query_one("SELECT status_changed_at FROM returns WHERE id = 'R-old'")
    assert row["status_changed_at"], "момент смены статуса не достали из raw"
    act = return_acts.get("spare1")
    day = store.local_day("2026-09-14T06:00:00+00:00")
    assert act["received_day"] == day
    # В заголовке теперь число получения, а не дата составления акта.
    assert return_acts.detail("spare1")["title"].startswith(
        f"Возвраты за {day[8:10]}.{day[5:7]}.{day[:4]}, акт №"
    )


def test_status_change_backfill_runs_once():
    from app import accounts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_CHANGED_FILLED,))
    db.init_db()
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, raw) VALUES(?,?,?,?)",
        (account_id, "R-later", 0,
         '{"visual": {"change_moment": "2026-09-14T06:00:00Z"}}'),
    )
    db.init_db()
    assert db.query_one("SELECT status_changed_at FROM returns WHERE id = 'R-later'")["status_changed_at"] is None


def test_unreceived_returns_are_released_from_spare_acts():
    """Обновление освобождает из актов то, что ещё не получено.

    Версия 1.26.1 сметала в акт «без статуса» любой возврат, ушедший из выдачи
    без «Получен», — в акте приёмки оказывался и тот, что едет к продавцу.
    Удалишь акт, а обновление кладёт его обратно.
    """
    from app import accounts, return_acts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_SPARES_RELEASED,))
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("spare-live", db.now_iso(), "nosheet", account_id),
    )
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, status_sys, act_id) VALUES(?,?,?,?,?)",
        (account_id, "R-transit", 0, "MovingToSeller", "spare-live"),
    )

    db.init_db()

    assert db.query_one("SELECT act_id FROM returns WHERE id = 'R-transit'")["act_id"] is None
    assert return_acts.get("spare-live") is None, "опустевший акт не убран"


def test_release_keeps_marked_rows_and_confirmed_acts():
    """Работу сборщика и закрытые акты не трогаем."""
    from app import accounts, return_acts

    account_id = accounts.default_account()["id"]
    db.execute("DELETE FROM kv WHERE key = ?", (db.KV_SPARES_RELEASED,))
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id) VALUES(?,?,?,?)",
        ("spare-marked", db.now_iso(), "nosheet", account_id),
    )
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, act_id, mark, note) VALUES(?,?,?,?,?,?)",
        (account_id, "R-marked2", 0, "spare-marked", "bad", "вскрыта упаковка"),
    )
    db.execute(
        "INSERT INTO return_acts(id, created_at, kind, account_id, confirmed_at) VALUES(?,?,?,?,?)",
        ("spare-done", db.now_iso(), "nosheet", account_id, db.now_iso()),
    )
    db.execute(
        "INSERT INTO returns(account_id, id, is_ready, act_id) VALUES(?,?,?,?)",
        (account_id, "R-closed", 0, "spare-done"),
    )

    db.init_db()

    assert db.query_one("SELECT act_id FROM returns WHERE id = 'R-marked2'")["act_id"] == "spare-marked"
    assert db.query_one("SELECT act_id FROM returns WHERE id = 'R-closed'")["act_id"] == "spare-done"
    assert return_acts.get("spare-done"), "подтверждённый акт удалён"
