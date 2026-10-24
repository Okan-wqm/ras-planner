"""Starting texts for a new proposal. Everything here is copied into the proposal when it is created
and can be edited there; nothing in this file is printed directly."""

COMPANY = {
    "name": "Suderra AS",
    "country": "Norway",
}

WORK_PACKAGES = [
    ("Production plan", "Production scenarios, daily feed, oxygen and water exchange calculations"),
    ("Equipment capacities, specifications and price list",
     "Pre-growing, hatchery, water intake, wastewater and heat exchange lines (RAS). Equipment of the water "
     "intake and wastewater treatment processes is supplied by third parties."),
    ("Equipment and process layout", "Pre-growing, hatchery, fresh water, wastewater and heat exchange lines (RAS)"),
    ("Fabrication designs", "Fish tanks, settling filters and tank water inlets (2D and 3D, with bill of materials)"),
    ("Heating and cooling system", "Mechanical installation design"),
    ("Ventilation and air conditioning (HVAC)", "Mechanical installation design"),
    ("Clean water and wastewater systems", "Mechanical installation design"),
    ("Gas distribution (RAS)", "Air, oxygen and ozone for the production systems"),
    ("Fire suppression system", "Mechanical installation design (whole building)"),
    ("Electrical installation", "Electrical design"),
    ("Earthing and surge protection", "Electrical design"),
    ("Bills of quantities", "All disciplines"),
    ("Clash analysis and coordination", "3D drawing (LOD 350) of equipment connected to the 3D piping; "
                                        "layout plans of all production units as 2D reports"),
    ("3D animation (60 s) and 4 renders", "3D video and renderings of the facility"),
    ("Investment cost (CAPEX)", "Main equipment costs (installation, customs, shipping and storage excluded)"),
    ("Operating cost (OPEX)", "Production cost for the production scenarios"),
]

TERMS = (
    "This proposal is valid until {valid_until}.\n\n"
    "The customer pays 30 % of the total price stated in the Pricing section in advance. The project is "
    "delivered within 60 working days of the advance payment. The remaining 70 % is paid in three equal "
    "instalments, one every two months, within six months of delivery.\n\n"
    "Unless stated otherwise, all files (except videos) are delivered as 2D reports (PDF) or as Excel files "
    "without formulas.\n\n"
    "Equipment is included in the project according to information received from third-party suppliers. "
    "{company} is not responsible for errors or delays caused by third parties."
)

PAYMENT = (
    "All prices are in {currency} and exclude VAT; VAT is added at the applicable rate.\n\n"
    "The customer pays the invoiced amounts by the due dates in section 2. For late payments {company} may "
    "charge interest on the overdue amount and any currency difference between the invoice date and the "
    "payment date.\n\n"
    "If the customer does not follow the payment plan before delivery, {company} may cancel the order and "
    "claim 20 % of the price as compensation. After delivery the customer remains liable for all instalments."
)

CLAUSES = [
    ("Intellectual property",
     "All drawings and technical documents delivered by {company}, before or after the contract is signed, "
     "remain the property of {company}. Without the written consent of {company} they may only be used for the "
     "purpose for which they were delivered, and may not be copied, reproduced, transferred or disclosed to "
     "third parties."),
    ("Retention of title",
     "Ownership of the products and services remains with {company} until they are paid in full."),
    ("Indirect losses",
     "Unless agreed otherwise in writing, {company} accepts no liability for loss of production, loss of profit, "
     "loss of use, loss of contract or any other indirect or consequential loss."),
    ("Compliance",
     "A proposal binds {company} only once an order based on it has been accepted in writing. This proposal may "
     "not be shared with third parties. The customer will not use the deliveries for any purpose prohibited by "
     "law, and will comply with applicable export control, customs and sanctions rules. {company} is not obliged "
     "to perform, and may terminate, any order that such rules prevent."),
    ("Fees and charges",
     "Any stamp duty, fees or charges arising from this agreement are paid by the customer."),
    ("Governing law and disputes",
     "This agreement is governed by the laws of Norway. Disputes are settled by the ordinary courts, "
     "with Oslo District Court as the agreed venue."),
]

SECTIONS = [
    ("intro", "Introduction"),
    ("terms", "Terms of the proposal"),
    ("pricing", "Pricing"),
    ("sizing", "System sizing"),
    ("plan", "Production plan"),
    ("process", "Process design"),
    ("capex", "Investment estimate (CAPEX)"),
    ("opex", "Operating cost estimate (OPEX)"),
    ("payment", "Payment terms"),
    ("clauses", "General conditions"),
]
