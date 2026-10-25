"""
generate_test_pdfs.py — Generates 3 realistic PDF documents on disk to test
the live Gemini multimodal extraction and deterministic linter pipeline:
  1. test_po.pdf        (US Buyer NetSuite Purchase Order)
  2. test_tally_inv.pdf (Indian Exporter Tally Prime Invoice with LUT/IGST noise & planted errors)
  3. test_pl.pdf        (Exporter Packing List with planted carton drift)

Run:
    python generate_test_pdfs.py
"""

from fpdf import FPDF


def create_po_pdf(filename: str = "test_po.pdf") -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "NetSuite ERP - Purchase Order", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(100, 6, "PO Number: PO-PCR-45012")
    pdf.cell(0, 6, "Status: Partially Received", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(100, 5, "Shipment Ref: SHP-2026-8841")
    pdf.cell(0, 5, "Terms / Incoterm: FOB Mundra | Currency: USD", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Vendor & Buyer blocks
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(95, 5, "Vendor (Exporter):")
    pdf.cell(0, 5, "Ship To / Importer:", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(95, 5, "Shreeji Textiles & Apparel Pvt. Ltd.")
    pdf.cell(0, 5, "Pacific Coast Retail Group LLC", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 5, "Plot 2301, GIDC Pandesara, Surat, Gujarat 394221, IN")
    pdf.cell(0, 5, "Los Angeles, CA, USA", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 5, "Port of Loading: INMUN1")
    pdf.cell(0, 5, "Port of Discharge: USLAX", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Table Header ( Intentionally place Line 3 first to test Gemini semantic ordering! )
    headers = ["SKU", "Description", "US HTS (10)", "PO Qty", "Prior", "Open Bal", "Unit Price", "Line Total"]
    widths = [32, 58, 24, 15, 14, 16, 16, 18]

    pdf.set_font("Helvetica", "B", 8)
    for h, w in zip(headers, widths):
        pdf.cell(w, 7, h, border=1)
    pdf.ln()

    rows = [
        ["PCR-M-TEE-NVY-L", "Men's 100% Combed Cotton Crewneck Tee (Knitted)", "6109.10.0012", "2000", "0", "2000", "4.50", "9000.00"],
        ["PCR-W-POLO-WHT-M", "Women's Pique Polo Shirt", "6106.10.0010", "5000", "3000", "2000", "6.00", "12000.00"],
        ["PCR-H-HOOD-GRY-XL", "Heavyweight Fleece Pullover Hoodie", "6110.20.2079", "1000", "0", "1000", "11.00", "11000.00"],
    ]

    pdf.set_font("Helvetica", "", 7.5)
    for r in rows:
        for val, w in zip(r, widths):
            pdf.cell(w, 7, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "Note: Split-shipment tolerance configured by buyer: +/- 5% of open balance.", new_x="LMARGIN", new_y="NEXT")
    pdf.output(filename)
    print(f"[OK] Created {filename}")


def create_tally_invoice_pdf(filename: str = "test_tally_inv.pdf") -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Times", "B", 13)
    pdf.cell(0, 6, "EXPORT COMMERCIAL INVOICE", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Times", "", 8)
    pdf.cell(0, 5, "SUPPLY MEANT FOR EXPORT UNDER LUT WITHOUT PAYMENT OF IGST", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # Exporter & Invoice Header Box
    pdf.set_font("Times", "", 8.5)
    pdf.cell(95, 5, "Exporter: Shreeji Textiles & Apparel Pvt. Ltd.", border="LTR")
    pdf.cell(0, 5, "Invoice No: SHR/EXP/26-27/0412 | Date: 14-Oct-2026", border="LTR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(95, 5, "Plot 2301, GIDC Pandesara, Surat, Gujarat 394221", border="LR")
    pdf.cell(0, 5, "Buyer's PO: PO-PCR-45012 | Ref: SHP-2026-8841", border="LR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(95, 5, "Country: IN | GSTIN: 24AABCS1429B1Z5 | IEC: 5214098211", border="LR")
    pdf.cell(0, 5, "Port of Loading: INMUN1 | Port of Discharge: USLAX", border="LR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(95, 5, "LUT ARN: AD240426009182Z", border="LBR")
    pdf.cell(0, 5, "Incoterm: FOB Mundra | Currency: USD", border="LBR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(0, 6, "Consignee: Pacific Coast Retail Group LLC, Los Angeles, CA, USA", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Line items table (with Tally domestic tax noise row included)
    headers = ["Sl", "Description of Goods", "HSN/SAC", "Quantity", "Rate (USD)", "Per", "Amount (USD)"]
    widths = [10, 82, 24, 20, 18, 12, 24]

    pdf.set_font("Times", "B", 8.5)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()

    # Sabotaged rows:
    # 1. HSN 6205.20.00 (clashes with 6109.10)
    # 2. Qty 2050 (+2.5% warn) and Rate 6.40 ($0.40 overcharge crit)
    # 3. Normal line math, but Weight table below has Net 680 > Gross 620
    inv_rows = [
        ["1", "100% COTTON MENS ROUND NECK T-SHIRT (KNITTED) BATCH #44A", "6205.20.00", "2000", "4.50", "PCS", "9000.00"],
        ["2", "LADIES COTTON PIQUE COLLAR T-SHIRT WHITE", "6106.10.00", "2050", "6.40", "PCS", "13120.00"],
        ["3", "FLEECE PULLOVER HOODIE HEAVY GREY XL", "6110.20.00", "1000", "11.00", "PCS", "11000.00"],
        # Tally domestic tax noise rows that extractor.py must ignore:
        ["", "IGST @ 0% (Export Under LUT/Bond) - E.&O.E.", "", "", "", "", "0.00"],
        ["", "Total Invoice Value (USD)", "", "5050", "", "", "33120.00"],
    ]

    pdf.set_font("Times", "", 8)
    for r in inv_rows:
        for val, w in zip(r, widths):
            pdf.cell(w, 6, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Times", "B", 9)
    pdf.cell(0, 6, "Weight & Carton Statement (Per Line Item)", new_x="LMARGIN", new_y="NEXT")

    w_headers = ["Line", "Item Reference", "Cartons (CTNS)", "Net Wt (KGS)", "Gross Wt (KGS)"]
    w_widths = [15, 85, 30, 30, 30]
    for h, w in zip(w_headers, w_widths):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()

    # Notice Line 3 has Net Wt 680.00 > Gross Wt 620.00 and 30 Cartons (Total 100 CTNS)
    w_rows = [
        ["Line 1", "100% COTTON MENS ROUND NECK T-SHIRT", "40", "380.00", "420.00"],
        ["Line 2", "LADIES COTTON PIQUE COLLAR T-SHIRT WHITE", "30", "410.00", "450.00"],
        ["Line 3", "FLEECE PULLOVER HOODIE HEAVY GREY XL", "30", "680.00", "620.00"],
        ["Total", "Total Cartons & Weight", "100", "1470.00", "1490.00"],
    ]
    pdf.set_font("Times", "", 8)
    for r in w_rows:
        for val, w in zip(r, w_widths):
            pdf.cell(w, 6, val, border=1)
        pdf.ln()

    pdf.ln(5)
    pdf.cell(0, 5, "Declaration: We declare that this invoice shows the actual price of the goods described. Authorised Signatory.", new_x="LMARGIN", new_y="NEXT")
    pdf.output(filename)
    print(f"[OK] Created {filename}")


def create_packing_list_pdf(filename: str = "test_pl.pdf") -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, "PACKING LIST", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(100, 5, "Packing List No: SHR/PL/0412 | Date: 14-Oct-2026")
    pdf.cell(0, 5, "Invoice Ref: SHR/EXP/26-27/0412 | PO: PO-PCR-45012", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Shipper: Shreeji Textiles & Apparel Pvt. Ltd., Surat | Consignee: Pacific Coast Retail Group LLC", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Port of Loading: INMUN1 | Port of Discharge: USLAX | Shipment Ref: SHP-2026-8841", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    headers = ["Line", "Description of Goods", "Cartons (CTNS)", "Net Wt (KGS)", "Gross Wt (KGS)"]
    widths = [15, 85, 30, 30, 30]

    pdf.set_font("Helvetica", "B", 8.5)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()

    # Notice Line 3 has 38 Cartons (Total 108 CTNS vs Invoice's 100 CTNS) and Net Wt 580.00 kg
    pl_rows = [
        ["1", "100% COTTON MENS ROUND NECK T-SHIRT (KNITTED) BATCH #44A", "40", "380.00", "420.00"],
        ["2", "LADIES COTTON PIQUE COLLAR T-SHIRT WHITE", "30", "410.00", "450.00"],
        ["3", "FLEECE PULLOVER HOODIE HEAVY GREY XL", "38", "580.00", "620.00"],
        ["TOTAL", "All Lines", "108", "1370.00", "1490.00"],
    ]

    pdf.set_font("Helvetica", "", 8)
    for r in pl_rows:
        for val, w in zip(r, widths):
            pdf.cell(w, 6, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.cell(0, 5, "Shipping Marks: PCR / LAX / C/NO. 1-108 | Country of Origin: Made in India", new_x="LMARGIN", new_y="NEXT")
    pdf.output(filename)
    print(f"[OK] Created {filename}")


if __name__ == "__main__":
    create_po_pdf()
    create_tally_invoice_pdf()
    create_packing_list_pdf()
    print("\nAll 3 test PDFs generated! Test your live FastAPI endpoint with:")
    print('  curl -X POST http://localhost:8000/api/audit -F "po_pdf=@test_po.pdf" -F "inv_pdf=@test_tally_inv.pdf" -F "pl_pdf=@test_pl.pdf"')