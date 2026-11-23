"""Documented CIS messages only; never display arbitrary API response text."""

STATUSES = {
    'FAILED': 'Не удалось проверить код. Повторите попытку позже или удалите код маркировки.',
    'IN_PROGRESS': 'В процессе проверки.',
    'NOT_ON_VALIDATION': 'Код маркировки не отправлен на проверку.',
    'OK': 'Проверка успешно пройдена.',
    'INVALID': 'Проверка не пройдена. Продажа товара с этим кодом запрещена.',
}

SUBSTATUSES = {
    'WRONG_OWNER_INN': 'Проверка не пройдена. ИНН владельца кода отличается от ИНН продавца.',
    'CIS_VALIDATION_ERROR': 'Проверка не пройдена.',
    'CIS_GTIN_NOT_FOUND': 'Код маркировки не содержит GTIN.',
    'CIS_SERIAL_NUMBER_NOT_FOUND': 'Код маркировки не содержит серийный номер.',
    'INVALID_SYMBOLS_FOUND': 'Код маркировки содержит недопустимые символы.',
    'CRYPTO_TAIL_FORMAT_MISMATCH_CIS_TYPE': 'Формат криптоподписи не соответствует типу кода маркировки.',
    'INVALID_CRYPTO_TAIL': 'Криптоподпись не валидна.',
    'INVALID_CRYPTO_KEY': 'Криптоключ не валиден.',
    'VERIFICATION_FAILED_IN_EMITTER_COUNTRY': 'Код маркировки не прошел верификацию в стране эмитента.',
    'UNSUPPORTED_AI_FOUND': 'Найденные в коде маркировки AI не поддерживаются.',
    'CIS_NOT_FOUND_IN_GIS_MT': 'Код маркировки не найден в ГИС МТ.',
    'NOT_PLACED_ON_MARKET': 'Код маркировки не введен в оборот.',
    'NOT_PRINTED_ON_PACKAGE': 'Код маркировки не нанесен на упаковку.',
    'EXPIRED_ITEM': 'У маркированного товара истек срок годности.',
    'SALE_BLOCKED_BY_OGB': 'Розничная продажа продукции заблокирована по решению ОГВ.',
    'ITEM_SOLD': 'Маркированный товар был продан.',
}


def describe(result: dict) -> dict:
    status = result.get('status')
    status = status if isinstance(status, str) and status in STATUSES else None
    substatus = result.get('substatus') if status == 'INVALID' else None
    substatus = substatus if isinstance(substatus, str) and substatus in SUBSTATUSES else None
    return {'status': status, 'substatus': substatus,
            'message': SUBSTATUSES[substatus] if substatus else STATUSES.get(status)}
