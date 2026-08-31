from __future__ import annotations

from math import ceil
from urllib.parse import urlencode


def pagination_context(
    path: str,
    page: int,
    per_page: int,
    total: int,
    **params: object,
) -> dict[str, object]:
    pages = max(ceil(total / per_page), 1)
    current = min(max(page, 1), pages)

    def page_url(number: int) -> str:
        values = {key: value for key, value in params.items() if value not in {None, ""}}
        if number > 1:
            values["page"] = number
        query = urlencode(values, doseq=True)
        return f"{path}?{query}" if query else path

    visible = {1, pages}
    visible.update(range(max(1, current - 2), min(pages, current + 2) + 1))
    links: list[dict[str, object]] = []
    previous_number = 0
    for number in sorted(visible):
        if previous_number and number - previous_number > 1:
            links.append({"ellipsis": True})
        links.append(
            {
                "number": number,
                "url": page_url(number),
                "current": number == current,
            }
        )
        previous_number = number

    return {
        "page": current,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "from_item": (current - 1) * per_page + 1 if total else 0,
        "to_item": min(current * per_page, total),
        "prev_url": page_url(current - 1) if current > 1 else None,
        "next_url": page_url(current + 1) if current < pages else None,
        "links": links,
    }
