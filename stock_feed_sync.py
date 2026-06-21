import argparse
import csv
import io
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    requests = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


REPORT_FIELDS = [
    "row_number",
    "sku",
    "input_stock",
    "status",
    "message",
    "unas_status",
    "unas_error",
]
BATCH_SIZE = 100
DEFAULT_UNAS_API_BASE_URL = "https://api.unas.eu/shop"
API_REQUEST_ATTEMPTS = 3
API_CONNECT_TIMEOUT = 30
API_READ_TIMEOUT = 60
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


class UnasApiError(Exception):
    pass


@dataclass
class Config:
    csv_url: str
    sku_column: str
    stock_column: str
    delimiter: str
    encoding: str
    report_dir: Path
    dry_run: bool
    live: bool
    limit: int | None
    unas_api_key: str | None
    unas_api_base_url: str


def read_config() -> Config:
    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Dry-run CSV stock feed validator for future UNAS setStock sync."
    )
    parser.add_argument("--csv-url", default=os.getenv("CSV_URL"))
    parser.add_argument("--sku-column", default=os.getenv("CSV_SKU_COLUMN", "sku"))
    parser.add_argument("--stock-column", default=os.getenv("CSV_STOCK_COLUMN", "stock"))
    parser.add_argument("--delimiter", default=os.getenv("CSV_DELIMITER", ","))
    parser.add_argument("--encoding", default=os.getenv("CSV_ENCODING", "utf-8"))
    parser.add_argument("--report-dir", default=os.getenv("REPORT_DIR", "reports"))
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Default mode. No UNAS API calls are made.",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Send valid stock rows to the UNAS setStock API.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of valid rows to send in live mode.",
    )
    args = parser.parse_args()

    if not args.csv_url:
        raise ValueError("Missing CSV URL. Set CSV_URL or pass --csv-url.")

    if len(args.delimiter) != 1:
        raise ValueError("CSV delimiter must be exactly one character.")

    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be a positive integer.")

    unas_api_key = os.getenv("UNAS_API_KEY")
    if args.live and not unas_api_key:
        raise ValueError("Missing UNAS_API_KEY. Set it before running with --live.")

    return Config(
        csv_url=args.csv_url,
        sku_column=args.sku_column,
        stock_column=args.stock_column,
        delimiter=args.delimiter,
        encoding=args.encoding,
        report_dir=Path(args.report_dir),
        dry_run=not args.live,
        live=args.live,
        limit=args.limit,
        unas_api_key=unas_api_key,
        unas_api_base_url=os.getenv("UNAS_API_BASE_URL", DEFAULT_UNAS_API_BASE_URL),
    )


def download_csv(csv_url: str, encoding: str) -> str:
    if requests is None:
        with urllib.request.urlopen(csv_url, timeout=30) as response:
            return response.read().decode(encoding)

    response = requests.get(csv_url, timeout=30)
    response.raise_for_status()
    response.encoding = encoding
    return response.text


def validate_row(row_number: int, sku_value: str, stock_value: str) -> dict:
    sku = (sku_value or "").strip()
    input_stock = (stock_value or "").strip()

    if not sku:
        return build_report_row(row_number, sku, input_stock, "error", "missing_sku")

    if not input_stock:
        return build_report_row(row_number, sku, input_stock, "error", "missing_stock")

    try:
        stock_number = int(input_stock)
    except ValueError:
        try:
            float(input_stock)
        except ValueError:
            return build_report_row(row_number, sku, input_stock, "error", "invalid_stock")

        return build_report_row(
            row_number, sku, input_stock, "error", "decimal_stock_not_allowed"
        )

    if stock_number < 0:
        return build_report_row(row_number, sku, input_stock, "error", "negative_stock")

    return build_report_row(row_number, sku, input_stock, "valid", "ready_for_update")


def build_report_row(
    row_number: int, sku: str, input_stock: str, status: str, message: str
) -> dict:
    return {
        "row_number": row_number,
        "sku": sku,
        "input_stock": input_stock,
        "status": status,
        "message": message,
        "unas_status": "",
        "unas_error": "",
    }


def check_duplicate_skus(report_rows: list[dict]) -> list[dict]:
    sku_counts = Counter(row["sku"] for row in report_rows if row["sku"])
    duplicate_skus = {sku for sku, count in sku_counts.items() if count > 1}

    for row in report_rows:
        if row["sku"] in duplicate_skus:
            row["status"] = "error"
            row["message"] = "duplicate_sku"

    return report_rows


def process_csv(csv_content: str, config: Config) -> list[dict]:
    csv_file = io.StringIO(csv_content)
    reader = csv.DictReader(csv_file, delimiter=config.delimiter)

    if reader.fieldnames is None:
        raise ValueError("CSV file is empty or has no header row.")

    missing_columns = [
        column
        for column in (config.sku_column, config.stock_column)
        if column not in reader.fieldnames
    ]
    if missing_columns:
        available_columns = ", ".join(reader.fieldnames)
        raise ValueError(
            "Missing required CSV column(s): "
            + ", ".join(missing_columns)
            + f". Available columns: {available_columns}"
        )

    report_rows = []
    for row_number, row in enumerate(reader, start=2):
        report_rows.append(
            validate_row(
                row_number=row_number,
                sku_value=row.get(config.sku_column, ""),
                stock_value=row.get(config.stock_column, ""),
            )
        )

    return check_duplicate_skus(report_rows)


def save_report(report_rows: list[dict], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_path = report_dir / f"stock_sync_report_{timestamp}.csv"

    with report_path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report_rows)

    return report_path


def chunk_rows(rows: list[dict], batch_size: int) -> list[list[dict]]:
    return [rows[index : index + batch_size] for index in range(0, len(rows), batch_size)]


def build_login_xml(api_key: str) -> str:
    params = ET.Element("Params")
    ET.SubElement(params, "ApiKey").text = api_key
    ET.SubElement(params, "WebshopInfo").text = "true"
    return xml_to_string(params)


def xml_to_string(root: ET.Element) -> str:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def post_xml(url: str, xml_string: str, headers: dict | None = None) -> str:
    request_headers = {"Content-Type": "application/xml; charset=utf-8"}
    if headers:
        request_headers.update(headers)

    if requests is not None:
        for attempt in range(1, API_REQUEST_ATTEMPTS + 1):
            try:
                response = requests.post(
                    url,
                    data=xml_string.encode("utf-8"),
                    headers=request_headers,
                    timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == API_REQUEST_ATTEMPTS:
                    raise UnasApiError(
                        f"Request failed after {attempt} attempts: {exc}"
                    ) from exc
                wait_before_retry(attempt, "network connection failed")
                continue
            except requests.RequestException as exc:
                raise UnasApiError(f"Request failed: {exc}") from exc

            if (
                response.status_code in RETRYABLE_HTTP_STATUSES
                and attempt < API_REQUEST_ATTEMPTS
            ):
                wait_before_retry(attempt, f"HTTP {response.status_code}")
                continue

            if response.status_code >= 400:
                raise UnasApiError(
                    f"HTTP {response.status_code}: {response.text[:500]}"
                )

            response.encoding = "utf-8"
            return response.text

    request = urllib.request.Request(
        url,
        data=xml_string.encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    for attempt in range(1, API_REQUEST_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=API_CONNECT_TIMEOUT
            ) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if (
                exc.code in RETRYABLE_HTTP_STATUSES
                and attempt < API_REQUEST_ATTEMPTS
            ):
                wait_before_retry(attempt, f"HTTP {exc.code}")
                continue
            raise UnasApiError(f"HTTP {exc.code}: {error_body[:500]}") from exc
        except urllib.error.URLError as exc:
            if attempt == API_REQUEST_ATTEMPTS:
                raise UnasApiError(
                    f"Request failed after {attempt} attempts: {exc}"
                ) from exc
            wait_before_retry(attempt, "network connection failed")

    raise UnasApiError("Request failed without a response.")


def wait_before_retry(attempt: int, reason: str) -> None:
    delay_seconds = 2**attempt
    print(
        f"UNAS request attempt {attempt}/{API_REQUEST_ATTEMPTS} failed "
        f"({reason}); retrying in {delay_seconds} seconds."
    )
    time.sleep(delay_seconds)


def unas_login(api_key: str, base_url: str) -> str:
    response_text = post_xml(f"{base_url.rstrip('/')}/login", build_login_xml(api_key))
    root = ET.fromstring(response_text)
    token = find_first_text(root, "Token")
    if not token:
        raise UnasApiError("UNAS login failed: Token was not found in the response.")
    return token


def build_set_stock_xml(valid_rows: list[dict]) -> str:
    products = ET.Element("Products")

    for row in valid_rows:
        product = ET.SubElement(products, "Product")
        ET.SubElement(product, "Action").text = "modify"
        ET.SubElement(product, "Sku").text = row["sku"]
        stocks = ET.SubElement(product, "Stocks")
        stock = ET.SubElement(stocks, "Stock")
        ET.SubElement(stock, "Qty").text = str(int(row["input_stock"]))

    return xml_to_string(products)


def send_set_stock(xml_string: str, token: str, base_url: str) -> str:
    return post_xml(
        f"{base_url.rstrip('/')}/setStock",
        xml_string,
        headers={"Authorization": f"Bearer {token}"},
    )


def parse_set_stock_response(response_text: str) -> list[dict]:
    root = ET.fromstring(response_text)
    product_elements = find_elements(root, "Product")
    if not product_elements:
        product_elements = [root]

    results = []
    for product in product_elements:
        sku = find_first_text(product, "Sku")
        status = find_first_text(product, "Status")
        if not sku and not status:
            continue

        results.append(
            {
                "sku": sku,
                "action": find_first_text(product, "Action"),
                "status": status,
                "error": find_first_text(product, "Error"),
            }
        )

    return results


def find_elements(root: ET.Element, tag_name: str) -> list[ET.Element]:
    return [element for element in root.iter() if strip_namespace(element.tag) == tag_name]


def find_first_text(root: ET.Element, tag_name: str) -> str:
    for element in root.iter():
        if strip_namespace(element.tag) == tag_name and element.text:
            return element.text.strip()
    return ""


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def apply_unas_result(row: dict, result: dict) -> None:
    unas_status = result.get("status", "")
    unas_error = result.get("error", "")
    row["unas_status"] = unas_status
    row["unas_error"] = unas_error

    if unas_status == "ok":
        row["status"] = "updated"
        row["message"] = "stock_updated"
    else:
        row["status"] = "error"
        row["message"] = "unas_error"


def mark_batch_http_error(batch: list[dict], error_message: str) -> None:
    for row in batch:
        row["status"] = "error"
        row["message"] = "unas_error"
        row["unas_status"] = "error"
        row["unas_error"] = error_message


def run_live_updates(report_rows: list[dict], config: Config) -> None:
    valid_rows = [row for row in report_rows if row["status"] == "valid"]
    rows_to_send = valid_rows[: config.limit] if config.limit else valid_rows

    if not rows_to_send:
        print("No valid rows to send to UNAS.")
        return

    if config.limit:
        print(f"Live mode limit: sending up to {config.limit} valid row(s).")
    else:
        print(
            "WARNING: live mode is running without --limit. "
            f"All {len(rows_to_send)} valid row(s) will be sent."
        )

    print("Logging in to UNAS API.")
    token = unas_login(config.unas_api_key or "", config.unas_api_base_url)
    print("UNAS login succeeded.")

    for batch_number, batch in enumerate(chunk_rows(rows_to_send, BATCH_SIZE), start=1):
        print(f"Sending batch {batch_number} with {len(batch)} product(s).")
        try:
            xml_string = build_set_stock_xml(batch)
            response_text = send_set_stock(
                xml_string, token, config.unas_api_base_url
            )
            parsed_results = parse_set_stock_response(response_text)
            results_by_sku = {result["sku"]: result for result in parsed_results}

            for row in batch:
                result = results_by_sku.get(row["sku"])
                if result:
                    apply_unas_result(row, result)
                else:
                    mark_batch_http_error(
                        [row], "No product result returned by UNAS for this SKU."
                    )
        except (UnasApiError, ET.ParseError, urllib.error.URLError) as exc:
            mark_batch_http_error(batch, str(exc))


def print_summary(report_rows: list[dict], report_path: Path, dry_run: bool) -> None:
    valid_count = sum(1 for row in report_rows if row["status"] == "valid")
    updated_count = sum(1 for row in report_rows if row["status"] == "updated")
    error_count = sum(1 for row in report_rows if row["status"] == "error")
    mode = "dry-run" if dry_run else "live"

    print(f"Mode: {mode}")
    print(f"UNAS API calls: {'disabled' if dry_run else 'enabled'}")
    print(f"Rows checked: {len(report_rows)}")
    print(f"Valid rows: {valid_count}")
    print(f"Updated rows: {updated_count}")
    print(f"Error rows: {error_count}")
    print(f"Report saved: {report_path}")


def main() -> int:
    try:
        config = read_config()
        print("Downloading CSV feed.")
        csv_content = download_csv(config.csv_url, config.encoding)
        report_rows = process_csv(csv_content, config)
        if config.live:
            run_live_updates(report_rows, config)
        report_path = save_report(report_rows, config.report_dir)
        print_summary(report_rows, report_path, config.dry_run)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"CSV download failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Configuration or CSV error: {exc}", file=sys.stderr)
        return 1
    except UnasApiError as exc:
        print(f"UNAS API error: {exc}", file=sys.stderr)
        return 1
    except ET.ParseError as exc:
        print(f"XML parse error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        if requests is not None and isinstance(exc, requests.RequestException):
            print(f"CSV download failed: {exc}", file=sys.stderr)
        else:
            print(f"File error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if requests is not None and isinstance(exc, requests.RequestException):
            print(f"CSV download failed: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
