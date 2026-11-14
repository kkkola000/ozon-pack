# ЧЗ и дополнительные сведения: проверка Ozon и Яндекс Маркета

Дата сверки: 14 ноября 2026. Проверяемая версия панели: 1.67.2.
Область: заказы «Ожидают сборки» и «Ожидают отгрузки».

## Вывод

В текущем коде смешаны три разных понятия: товар допускает маркировку,
маркировку обязательно передать сейчас, и код уже передан и принят площадкой.
ГТД, РНПТ и УИН — отдельные типы дополнительных сведений.
Отсутствие «М» сейчас не доказывает, что площадка не поддерживает/не требует ЧЗ.

Версия 1.67.2 на сервере подтверждена пользователем. При этом реальные ответы
API отправления 51121561-0339-2 и SKU 6346622543 в этой облачной среде недоступны:
здесь нет производственных ключей кабинета. Скриншот показывает состояние
интерфейса, но не значения полей API. Точная причина для этого отправления
требует фактического ответа, а не очередного предположения о флагах.

## Ozon

Метод `/v3/posting/fbs/get` возвращает `requirements`: списки SKU, которым
нужно передать сведения для следующего статуса:

- `products_requiring_mandatory_mark` — ЧЗ;
- `products_requiring_gtd` — ГТД;
- `products_requiring_rnpt` — РНПТ;
- отдельные списки для веса, IMEI, УИН и страны.

Поле `products[].mandatory_mark` помечено в схеме как устаревшее. Поле
`available_actions` перечисляет доступные действия, включая
`ship_with_additional_info` и `update_cis`. Оно не является перечнем
маркируемых SKU. В описании нет гарантии, что у каждого товара с доступным
ЧЗ в обоих статусах обязательно будет одно из этих действий.

Метод `/v6/fbs/posting/product/exemplar/create-or-get` содержит сведения
по продуктам и экземплярам:

| Поле продукта | Смысл по схеме |
| --- | --- |
| `is_mandatory_mark_needed` | Необходимо передать ЧЗ |
| `is_mandatory_mark_possible` | Возможно заполнить ЧЗ |
| `is_gtd_needed` | Необходимо передать ГТД |
| `is_rnpt_needed` | Необходимо передать РНПТ |
| `is_jw_uin_needed`, `has_imei`, `is_weight_needed` | Другие данные экземпляра |

Данные экземпляров находятся в `exemplars[]`: `marks`, `gtd`,
`is_gtd_absent`, `rnpt`, `is_rnpt_absent`, `weight`. Наличие кода и необходимость
передать код — разные сведения. В определении `ExemplarMark.mark_type`
перечислены `mandatory_mark`, `jw_uin`, `imei`.

Для уже собранного отправления применяется последовательность:
`/v6/fbs/posting/product/exemplar/set` → проверка
`/v5/fbs/posting/product/exemplar/status` →
`/v1/fbs/posting/product/exemplar/update` для сохранения обновлённых данных
в статусе «Ожидает отгрузки». HTTP 200 метода set означает создание задачи.
Считывать требования и имеющиеся данные нужно в обоих рабочих статусах:
`awaiting_packaging` и `awaiting_deliver`.

Прямой сайт docs.ozon.ru при проверке возвращал цикл перенаправлений.
Поля сверены по опубликованной копии схемы Seller API:
[схема](https://github.com/vispar-tech/ozon-seller-api-schema/blob/main/schemas/ozon-seller-api-openapi.json).
Оригинальные ссылки:
[create-or-get](https://docs.ozon.ru/api/seller/#operation/PostingAPI_FbsPostingProductExemplarCreateOrGetV6),
[update](https://docs.ozon.ru/api/seller/#operation/PostingAPI_FbsPostingProductExemplarUpdateV1).

## Яндекс Маркет

Официальная схема `POST /v1/businesses/{businessId}/orders` возвращает
`items[].requiredInstanceTypes`:

| Значение | Смысл |
| --- | --- |
| `CIS` | ЧЗ обязателен |
| `CIS_OPTIONAL` | ЧЗ необязателен |
| `GTD` | ГТД |
| `RNPT` | РНПТ |
| `UIN` | УИН ювелирного изделия |

Переданные сведения находятся в `items[].instances[]`: `cis`, `cisFull`,
`gtd`, `rnpt`, `uin`. `cisFull` содержит криптохвост. Если магазин ещё не
передавал коды, `instances` может отсутствовать: по этому отсутствию нельзя
делать вывод, что товар не требует сведений.

Нужные стадии: `status=PROCESSING` с `substatus=STARTED` и `READY_TO_SHIP`.
Чтение заказа доступно по его ID для обеих стадий.

Для FBS/Экспресс передача кодов выполняется через
`PUT /v2/campaigns/{campaignId}/orders/{orderId}/boxes`, проверка —
`POST /v2/campaigns/{campaignId}/orders/{orderId}/identifiers/status`.
Изменять переданную раскладку схема разрешает до `READY_TO_SHIP`.
Метод `/identifiers` предназначен для DBS; его нельзя считать обходом
ограничения FBS. Нельзя приравнивать распознанный тип товара к разрешению
на запись кодов в любом статусе.

Источники из официального репозитория Яндекс Маркета:

- [BusinessOrderItemDTO](https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/components/schemas/BusinessOrderItemDTO.yaml)
- [OrderItemInstanceType](https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/components/schemas/OrderItemInstanceType.yaml)
- [OrderItemInstanceDTO](https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/components/schemas/OrderItemInstanceDTO.yaml)
- [setOrderBoxLayout](https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/paths/v2_campaigns_campaignId_orders_orderId_boxes.yaml)
- [provideOrderItemIdentifiers](https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/paths/v2_campaigns_campaignId_orders_orderId_identifiers.yaml)

## Пробелы в коде 1.67.2

1. `app/markets/ozon/marking.py::prepare` по-прежнему может завершиться
   до запроса экземпляров, если нет известных требований и предварительных
   признаков в posting/get. Содержимое v6 в таком случае не проверяется.
2. Там же данные с `required=false`, `possible=true` отбрасываются, если
   раньше не было маркировки и это не явное сканирование ЧЗ. Это скрывает
   доступную маркировку у товара, даже когда экземпляры уже прочитаны.
3. `app/core/marking.py::enrich` не добавляет «М» для необязательной маркировки
   без локально сохранённого кода. Это касается и `CIS_OPTIONAL` Яндекса.
4. В нормализованных продуктах сохраняются только `required`/`possible` ЧЗ.
   Признаки необходимости ГТД/РНПТ остаются в исходном контексте или вообще
   не сохраняются, если контекст отброшен как не требующий ЧЗ. Отдельной
   нормализованной модели этих требований и их состояния в сборке нет.

Правильная модель должна раздельно хранить тип сведений, обязательность,
возможность заполнения, наличие сохранённых данных и результат проверки.
«М» относится к ЧЗ. Нужную только ГТД нельзя заменять требованием скана ЧЗ.
Обязательный ЧЗ блокирует завершение; доступный необязательный код не должен
автоматически становиться обязательным. Зелёный статус требует проверки
конкретных кодов, а не одного только наличия `possible=true`.

## Диагностика реального заказа

Добавлен модуль, который запрашивает фактические сведения независимо от
условного пропуска v6 в рабочем процессе. Он не вызывает ship, set, update,
изменение коробок или статуса. Для Ozon вызывается create-or-get, который
предназначен для получения/создания идентификаторов экземпляров.

Запуск на сервере из каталога панели:

```bash
cd /opt/ozon-pack
sudo -u ozon .venv/bin/python -m app.order_requirements_diagnostics --market ozon --order 51121561-0339-2
```

Для Яндекса:

```bash
sudo -u ozon .venv/bin/python -m app.order_requirements_diagnostics --market yandex --order НОМЕР_ЗАКАЗА
```

При нескольких подходящих кабинетах укажите `--account-id ЧИСЛО`.
Используются уже настроенные ключи панели. В JSON выводятся версия,
статус, требуемые типы, флаги по SKU и количество имеющихся данных.
Пароли, ключи, полные ЧЗ, номера ГТД, адреса и данные покупателя не выводятся.
Ошибки API представлены классом ошибки и HTTP-статусом без тела ответа.
Перезапуск службы для запуска этого диагностического модуля не требуется.
