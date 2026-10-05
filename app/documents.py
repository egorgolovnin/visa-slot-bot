"""Official document lists per submission system (only links checked against the official sites)."""
from __future__ import annotations

# mission -> list of (label, url)
DOC_LINKS: dict[str, list[tuple[str, str]]] = {
    "hun": [
        ("Список документов (туризм)", "https://visa.vfsglobal.com/one-pager/hungary/cyprus/english/pdf/New-Tourist-checklist.pdf"),
        ("Анкета на визу", "https://visa.vfsglobal.com/one-pager/hungary/cyprus/english/pdf/application_form_correct.pdf"),
        ("Вся информация VFS: сборы, фото, сроки", "https://visa.vfsglobal.com/one-pager/hungary/cyprus/english/"),
    ],
    "aut": [
        ("Документы, сборы, анкета (VFS)", "https://visa.vfsglobal.com/one-pager/austria/cyprus/english/"),
        ("Требования посольства Австрии", "https://www.bmeia.gv.at/en/austrian-embassy-nicosia/travels-to-austria/entrance-and-residence/how-to-apply"),
    ],
    "fin": [("Документы, сборы, анкета (VFS)", "https://visa.vfsglobal.com/one-pager/Finland/Cyprus/English/")],
    "svk": [("Порядок подачи и онлайн-анкета (посольство Словакии)",
             "https://www.mzv.sk/en/web/nikozia-en/services/visa-information/schengen-visa"),
            ("Страница визового центра (VFS)", "https://visa.vfsglobal.com/cyp/en/svk")],
    "hrv": [("Документы и сборы (VFS)", "https://visa.vfsglobal.com/one-pager/croatia/cyprus/english/"),
            ("Онлайн-анкета на визу (МИД Хорватии)", "https://crovisa.mvep.hr")],
    "deu": [("Шенгенская виза: документы и правила (посольство Германии)", "https://nikosia.diplo.de/cy-en/service/1672100-1672100")],
    "are-fra": [("Анкета France-Visas (сначала она, потом запись)", "https://france-visas.gouv.fr"),
                ("Визовый центр Франции в ОАЭ (VFS)", "https://visa.vfsglobal.com/are/en/fra")],
    "are-ita": [("Визовый центр Италии в ОАЭ (VFS)", "https://visa.vfsglobal.com/are/en/ita")],
    "are-deu": [("Визовый центр Германии в ОАЭ (VFS)", "https://visa.vfsglobal.com/are/en/deu")],
    "srb-hrv": [("Визовый центр Хорватии в Сербии (VFS)", "https://visa.vfsglobal.com/srb/en/hrv"),
                ("Онлайн-анкета на визу (МИД Хорватии)", "https://crovisa.mvep.hr")],
}
# destination-specific extras (countries whose visas the German embassy issues)
DESTINATION_EXTRA: dict[str, list[tuple[str, str]]] = {
    "nld": [("Нидерланды: подача с Кипра", "https://www.netherlandsworldwide.nl/visa-the-netherlands/schengen-visa/apply-cyprus")],
}

PROCESS = (
    "<b>Как проходит запись</b>\n"
    "1. Выбираешь страну и даты, когда можешь подать документы\n"
    "2. Отправляешь данные паспорта (для Австрии и Венгрии — ещё фото разворота)\n"
    "3. Если визовый центр берёт сбор за запись — оплачиваешь его сразу, мы платим его за тебя при брони\n"
    "4. Мы ловим свободный слот и записываем тебя: пришлём дату, время, адрес и PDF-подтверждение\n"
    "5. Оплачиваешь наш сервис — {price} €\n"
    "6. Приходишь в визовый центр в назначенное время с документами\n"
    "7. Получаешь визу и кайфуешь 🌴\n\n"
    "⚠️ Визовые центры выпускают слоты почти накануне: запись может прийти всего за 2–7 дней до подачи. "
    "Документы, на которые нужно время (справки, выписки, страховка), готовь заранее.\n\n"
    "📄 Документы на визу собираешь сам по официальному списку — ссылки пришлём после выбора страны.\n"
    "📂 Не хочешь разбираться? Соберём весь пакет документов за тебя — <b>{docs_price} €</b>."
)


def links_for(mission: str, destination: str | None = None) -> list[tuple[str, str]]:
    links = DOC_LINKS.get(mission)
    if links is None and "-" in mission:  # VFS centre abroad: its own page has documents and fees
        c, m = mission.split("-", 1)
        links = [("Документы, сборы и правила визового центра (VFS)", f"https://visa.vfsglobal.com/{c}/en/{m}")]
    return (links or []) + DESTINATION_EXTRA.get(destination or "", [])


def links_html(mission: str, destination: str | None = None) -> str:
    links = links_for(mission, destination)
    return "\n".join(f'• <a href="{url}">{label}</a>' for label, url in links)
