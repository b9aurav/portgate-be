"""
generate_stress_pdfs.py — Week 2 Stress-Test Packet Generator (Morbi Ceramics)
Fixed column widths and font sizes so zero text overflows into adjacent cells.
"""

from fpdf import FPDF


def create_stress_po(filename: str = "stress_po.pdf") -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "SAP Business One - Import Purchase Order", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(100, 6, "PO Number: PO-ATL-90210")
    pdf.cell(0, 6, "Status: Open / Split Delivery", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(100, 5, "Shipment Ref: SHP-2026-9915")
    pdf.cell(0, 5, "Incoterm: FOB Mundra | Currency: USD", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(95, 5, "Vendor (Manufacturer):")
    pdf.cell(0, 5, "Ship To / Importer of Record:", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(95, 5, "Royal Vitrified Ceramics Pvt. Ltd.")
    pdf.cell(0, 5, "Atlantic Stone & Tile Distributors Inc.", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 5, "Survey 812, NH-8A Lalpar, Morbi, Gujarat 363642, IN")
    pdf.cell(0, 5, "Savannah, GA, USA", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 5, "Port of Loading: INMUN1")
    pdf.cell(0, 5, "Port of Discharge: USSAV", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    headers = ["SKU", "Description", "US HTS (10)", "PO Qty", "Prior", "Open Bal", "Unit Price", "Open Total"]
    widths = [28, 64, 23, 14, 14, 15, 16, 18]

    pdf.set_font("Helvetica", "B", 7.5)
    for h, w in zip(headers, widths):
        pdf.cell(w, 7, h, border=1)
    pdf.ln()

    po_rows = [
        ["AST-SUB-WHT-36", "Glazed Ceramic Subway Wall Tile 3x6 White", "6907.23.9051", "3000", "0", "3000", "8.00", "24000.00"],
        ["AST-POR-STA-24", "Polished Porcelain Floor Tile 24x24 Statuario", "6907.21.9051", "2000", "1000", "1000", "12.50", "12500.00"],
        ["AST-VIT-NER-12", "Matte Vitrified Mosaic Tile 12x12 Nero Marquina", "6907.22.9051", "1500", "1500", "0", "15.00", "0.00"],
        ["AST-TER-RUS-18", "Unvarnished Rustic Terracotta Patio Paver 18x18", "6904.90.0000", "500", "0", "500", "10.00", "5000.00"],
    ]

    pdf.set_font("Helvetica", "", 7)
    for r in po_rows:
        for val, w in zip(r, widths):
            pdf.cell(w, 7, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "Buyer Tolerance Policy: Split shipments allowed up to +/- 5% of open balance.", new_x="LMARGIN", new_y="NEXT")
    pdf.output(filename)
    print(f"[OK] Re-created {filename}")


def create_stress_invoice(filename: str = "stress_tally_inv.pdf") -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Times", "B", 13)
    pdf.cell(0, 6, "TAX / EXPORT COMMERCIAL INVOICE", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Times", "", 8)
    pdf.cell(0, 5, "SUPPLY MEANT FOR EXPORT UNDER BOND OR LUT WITHOUT PAYMENT OF IGST", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    pdf.set_font("Times", "", 8.5)
    pdf.cell(95, 5, "Exporter: Royal Vitrified Ceramics Pvt. Ltd.", border="LTR")
    pdf.cell(0, 5, "Invoice No: RVC/EXP/26-27/1108 | Date: 27-Oct-2026", border="LTR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(95, 5, "Survey 812, NH-8A Lalpar, Morbi, Gujarat 363642", border="LR")
    pdf.cell(0, 5, "Buyer PO Ref: PO-ATL-90210 | Shipment Ref: SHP-2026-9915", border="LR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(95, 5, "Country: IN | GSTIN: 24AABCR8812C1Z9 | IEC: 2411009988", border="LR")
    pdf.cell(0, 5, "Port of Loading: INMUN1 | Port of Discharge: USSAV", border="LR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(95, 5, "LUT ARN: ", border="LBR")
    pdf.cell(0, 5, "Incoterm: CIF Savannah | Currency: USD", border="LBR", new_x="LMARGIN", new_y="NEXT")

    pdf.cell(0, 6, "Consignee: Atlantic Stone & Tile Distributors Inc., Savannah, GA, USA", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    headers = ["Sl", "Description of Goods", "HSN/SAC", "Qty", "Rate (USD)", "Per", "Amount (USD)"]
    widths = [8, 92, 22, 16, 18, 10, 24]

    pdf.set_font("Times", "B", 8)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()

    inv_rows = [
        ["1", "VITRIFIED MATTE MOSAIC TILE 300X300 NERO MARQUINA", "6907.22.00", "200", "15.00", "BOX", "3000.00"],
        ["2", "CERAMIC GLAZED SUBWAY WALL TILE 75X150 GLOSSY WHITE", "6908.90.00", "3000", "8.00", "BOX", "24000.00"],
        ["3", "RUSTIC TERRACOTTA EXTERIOR PATIO PAVER 450X450 CLAY", "6904.90.00", "500", "9.50", "BOX", "4850.00"],
        ["4", "POLISHED PORCELAIN VITRIFIED FLOOR TILE 600X600 STATUARIO", "6907.21.00", "1120", "12.50", "BOX", "14000.00"],
        ["", "CGST @ 0.00% (Domestic Exempt)", "", "", "", "", "0.00"],
        ["", "SGST @ 0.00% (Domestic Exempt)", "", "", "", "", "0.00"],
        ["", "IGST @ 0.00% (Supply Meant For Export) - E.&O.E.", "", "", "", "", "0.00"],
        ["", "Total Invoice Value (USD)", "", "4820", "", "", "45850.00"],
    ]

    pdf.set_font("Times", "", 7)
    for r in inv_rows:
        for val, w in zip(r, widths):
            pdf.cell(w, 6, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Times", "B", 9)
    pdf.cell(0, 6, "Weight & Pallet/Carton Summary (Listed in Invoice Sl Order)", new_x="LMARGIN", new_y="NEXT")

    w_headers = ["Inv Sl", "Goods Description", "Cartons (CTNS)", "Net Wt (KGS)", "Gross Wt (KGS)"]
    w_widths = [14, 92, 28, 28, 28]
    pdf.set_font("Times", "B", 8)
    for h, w in zip(w_headers, w_widths):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()

    w_rows = [
        ["Sl 1", "NERO MARQUINA MOSAIC 12X12", "20", "2400.00", "2500.00"],
        ["Sl 2", "SUBWAY WALL TILE WHITE 3X6", "150", "18000.00", "18600.00"],
        ["Sl 3", "RUSTIC TERRACOTTA PAVER 18X18", "50", "9200.00", "8800.00"],
        ["Sl 4", "STATUARIO FLOOR TILE 24X24", "112", "15680.00", "16200.00"],
        ["Total", "All Invoice Lines", "332", "45280.00", "46100.00"],
    ]
    pdf.set_font("Times", "", 7.5)
    for r in w_rows:
        for val, w in zip(r, w_widths):
            pdf.cell(w, 6, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.cell(0, 5, "AD Code: 0510002-6000009 | Bank: State Bank of India, Morbi Industrial Branch | Authorised Signatory", new_x="LMARGIN", new_y="NEXT")
    pdf.output(filename)
    print(f"[OK] Re-created {filename}")


def create_stress_pl(filename: str = "stress_pl.pdf") -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, "EXPORT PACKING LIST", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(100, 5, "Packing List No: RVC/PL/1108 | Date: 27-Oct-2026")
    pdf.cell(0, 5, "Invoice Ref: RVC/EXP/26-27/1108 | PO: PO-ATL-90210", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Exporter: Royal Vitrified Ceramics Pvt. Ltd., Morbi | Importer: Atlantic Stone & Tile Distributors Inc.", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Port of Loading: INMUN1 | Port of Discharge: USSAV | Ref: SHP-2026-9915", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    headers = ["PL Row", "Description of Goods", "Cartons (CTNS)", "Net Wt (KGS)", "Gross Wt (KGS)"]
    widths = [14, 92, 28, 28, 28]

    pdf.set_font("Helvetica", "B", 8)
    for h, w in zip(headers, widths):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()

    pl_rows = [
        ["Row 1", "POLISHED PORCELAIN VITRIFIED FLOOR TILE 600X600 STATUARIO", "112", "15680.00", "16200.00"],
        ["Row 2", "RUSTIC TERRACOTTA EXTERIOR PATIO PAVER 450X450 CLAY", "55", "8500.00", "8800.00"],
        ["Row 3", "CERAMIC GLAZED SUBWAY WALL TILE 75X150 GLOSSY WHITE", "150", "18000.00", "18600.00"],
        ["Row 4", "VITRIFIED MATTE MOSAIC TILE 300X300 NERO MARQUINA", "20", "2400.00", "2500.00"],
        ["TOTAL", "Total Shipment", "337", "44580.00", "46100.00"],
    ]

    pdf.set_font("Helvetica", "", 7)
    for r in pl_rows:
        for val, w in zip(r, widths):
            pdf.cell(w, 6, val, border=1)
        pdf.ln()

    pdf.ln(4)
    pdf.cell(0, 5, "Fumigation: ISPM-15 Heat Treated Wooden Pallets | Origin: Made in India", new_x="LMARGIN", new_y="NEXT")
    pdf.output(filename)
    print(f"[OK] Re-created {filename}")


if __name__ == "__main__":
    create_stress_po()
    create_stress_invoice()
    create_stress_pl()