"""Static reference data used by the generator.

Towns, ZIP codes and tariff codes are public reference information used only to
make the synthetic data feel real. Every person, account, address and dollar
amount the generator produces is fictional.
"""

# Service divisions (synthetic grouping of the service territory)
DIVISIONS = ["Metro", "Palisades", "Central", "Southern"]

# (city, county, zip, division, weight, lat, lon)
TOWNS = [
    ("Newark", "Essex", "07102", "Metro", 14, 40.7357, -74.1724),
    ("East Orange", "Essex", "07017", "Metro", 4, 40.7673, -74.2049),
    ("Irvington", "Essex", "07111", "Metro", 3, 40.7323, -74.2349),
    ("Bloomfield", "Essex", "07003", "Metro", 3, 40.8068, -74.1854),
    ("Montclair", "Essex", "07042", "Metro", 3, 40.8259, -74.2090),
    ("West Orange", "Essex", "07052", "Metro", 3, 40.7987, -74.2390),
    ("Jersey City", "Hudson", "07302", "Metro", 12, 40.7178, -74.0431),
    ("Bayonne", "Hudson", "07002", "Metro", 4, 40.6687, -74.1143),
    ("Hoboken", "Hudson", "07030", "Metro", 5, 40.7440, -74.0324),
    ("Union City", "Hudson", "07087", "Metro", 4, 40.7676, -74.0327),
    ("West New York", "Hudson", "07093", "Metro", 3, 40.7879, -74.0143),
    ("Kearny", "Hudson", "07032", "Metro", 2, 40.7684, -74.1454),
    ("Elizabeth", "Union", "07201", "Metro", 7, 40.6640, -74.2107),
    ("Linden", "Union", "07036", "Metro", 3, 40.6220, -74.2446),
    ("Plainfield", "Union", "07060", "Metro", 3, 40.6337, -74.4074),
    ("Westfield", "Union", "07090", "Metro", 2, 40.6590, -74.3474),
    ("Hackensack", "Bergen", "07601", "Palisades", 3, 40.8859, -74.0435),
    ("Teaneck", "Bergen", "07666", "Palisades", 3, 40.8976, -74.0160),
    ("Fort Lee", "Bergen", "07024", "Palisades", 3, 40.8509, -73.9701),
    ("Englewood", "Bergen", "07631", "Palisades", 2, 40.8929, -73.9726),
    ("Paramus", "Bergen", "07652", "Palisades", 2, 40.9445, -74.0754),
    ("Fair Lawn", "Bergen", "07410", "Palisades", 2, 40.9404, -74.1318),
    ("Garfield", "Bergen", "07026", "Palisades", 2, 40.8815, -74.1132),
    ("Paterson", "Passaic", "07501", "Palisades", 8, 40.9168, -74.1718),
    ("Clifton", "Passaic", "07011", "Palisades", 5, 40.8584, -74.1638),
    ("Passaic", "Passaic", "07055", "Palisades", 4, 40.8568, -74.1285),
    ("Wayne", "Passaic", "07470", "Palisades", 3, 40.9254, -74.2766),
    ("New Brunswick", "Middlesex", "08901", "Central", 3, 40.4862, -74.4518),
    ("Edison", "Middlesex", "08817", "Central", 5, 40.5187, -74.4121),
    ("Piscataway", "Middlesex", "08854", "Central", 3, 40.5549, -74.4643),
    ("Woodbridge", "Middlesex", "07095", "Central", 5, 40.5576, -74.2846),
    ("Franklin Township", "Somerset", "08873", "Central", 3, 40.4926, -74.4860),
    ("Trenton", "Mercer", "08608", "Central", 5, 40.2206, -74.7597),
    ("Hamilton", "Mercer", "08610", "Central", 5, 40.2115, -74.6796),
    ("Ewing", "Mercer", "08618", "Central", 2, 40.2698, -74.7999),
    ("Princeton", "Mercer", "08540", "Central", 2, 40.3573, -74.6672),
    ("Camden", "Camden", "08102", "Southern", 4, 39.9259, -75.1196),
    ("Cherry Hill", "Camden", "08002", "Southern", 5, 39.9348, -75.0307),
    ("Pennsauken", "Camden", "08110", "Southern", 2, 39.9562, -75.0580),
    ("Gloucester Township", "Camden", "08012", "Southern", 3, 39.7940, -75.0510),
    ("Collingswood", "Camden", "08108", "Southern", 1, 39.9182, -75.0713),
    ("Moorestown", "Burlington", "08057", "Southern", 2, 39.9689, -74.9488),
    ("Willingboro", "Burlington", "08046", "Southern", 2, 40.0279, -74.8690),
    ("Burlington", "Burlington", "08016", "Southern", 1, 40.0712, -74.8649),
    ("Deptford", "Gloucester", "08096", "Southern", 2, 39.8318, -75.1213),
]

AREA_CODES = {
    "Essex": "973", "Hudson": "201", "Union": "908", "Bergen": "201", "Passaic": "973",
    "Middlesex": "732", "Somerset": "908", "Mercer": "609", "Camden": "856",
    "Burlington": "609", "Gloucester": "856",
}

# code: (commodity, rate_class, description, load_profile, base_units_per_month,
#        customer_charge, delivery_per_unit, supply_per_unit)
RATES = {
    "RS": ("ELEC", "RES", "Residential Service", "RES", 620, 6.25, 0.0870, 0.1320),
    "RHS": ("ELEC", "RES", "Residential Heating Service", "RES_HEAT", 950, 6.25, 0.0790, 0.1300),
    "RLM": ("ELEC", "RES", "Residential Load Management", "RES", 1150, 17.50, 0.0910, 0.1340),
    "GLP": ("ELEC", "COM", "General Lighting and Power", "COM", 3400, 5.00, 0.0680, 0.1250),
    "LPL-S": ("ELEC", "COM", "Large Power and Lighting - Secondary", "COM", 42000, 395.00, 0.0360, 0.1120),
    "LPL-P": ("ELEC", "IND", "Large Power and Lighting - Primary", "IND", 175000, 780.00, 0.0240, 0.1060),
    "HTS": ("ELEC", "IND", "High Tension Service", "IND", 850000, 2450.00, 0.0120, 0.1010),
    "RSG": ("GAS", "RES", "Residential Service Gas", "GAS_RES", 72, 9.25, 0.5600, 0.4200),
    "GSG": ("GAS", "COM", "General Service Gas", "GAS_COM", 420, 21.00, 0.4600, 0.4000),
    "LVG": ("GAS", "IND", "Large Volume Gas", "GAS_IND", 6200, 155.00, 0.2500, 0.3800),
}
PROFILES = ["RES", "RES_HEAT", "COM", "IND", "GAS_RES", "GAS_COM", "GAS_IND"]

SEGMENTS = ["RES", "SMB", "CI", "GOV"]

FIRST_NAMES = """James Mary Robert Patricia John Jennifer Michael Linda David Elizabeth William Barbara
Richard Susan Joseph Jessica Thomas Sarah Christopher Karen Charles Lisa Daniel Nancy Matthew Betty
Anthony Sandra Mark Margaret Donald Ashley Steven Kimberly Andrew Emily Paul Donna Joshua Michelle
Kenneth Carol Kevin Amanda Brian Melissa George Deborah Timothy Stephanie Jose Rebecca Luis Maria
Carlos Ana Juan Rosa Wei Mei Raj Priya Anil Sunita Kwame Ama Olu Ngozi Andre Keisha Marcus Tanya
Giovanni Francesca Sean Siobhan Hyun Ji Omar Fatima Dmitri Olga Tomasz Agnieszka""".split()

LAST_NAMES = """Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez Martinez Hernandez Lopez
Gonzalez Wilson Anderson Thomas Taylor Moore Jackson Martin Lee Perez Thompson White Harris Sanchez
Clark Ramirez Lewis Robinson Walker Young Allen King Wright Scott Torres Nguyen Hill Flores Green
Adams Nelson Baker Hall Rivera Campbell Mitchell Carter Roberts Patel Shah Kim Park Chen Wang Singh
Okafor Mensah Adeyemi Russo Esposito Romano Colombo Murphy Kelly Sullivan OBrien Kowalski Nowak
Cohen Levy Ali Khan Castillo Morales Ortiz Gutierrez Reyes Cruz Santos Pereira Silva Rossi""".split()

STREETS = """Main Broad Park Washington Market Elm Maple Oak Pine Cedar Chestnut Walnut Spruce Highland
Central Lincoln Franklin Madison Jefferson Grove Prospect Summit Union Church Mill River Lake
Hillside Valley Orchard Sunset Ridge Forest Meadow Willow Garden Spring Liberty Hamilton Kennedy""".split()
STREET_SUFFIX = ["St", "Ave", "Rd", "Blvd", "Pl", "Ter", "Dr", "Ln", "Ct"]

BIZ_PREFIX = """Garden State|Liberty|Jersey|Shore|Summit|Hudson|Riverside|Keystone|Pioneer|Atlantic|
Meadowlands|Crossroads|Parkway|Cornerstone|Northgate|Brookside|Pinnacle|Metro|Palisade|Bridgeview|
Union Square|Oak Hill|Lakeside|Heritage|Ironbound|Harborview|Greenway|Main Street""".replace("\n", "").split("|")
SMB_TYPES = ["Deli", "Auto Body", "Dental Group", "Pharmacy", "Bakery", "Laundromat", "Diner", "Fitness",
             "Hardware", "Realty", "Medical Associates", "Pizzeria", "Salon", "Printing", "Plumbing",
             "Liquors", "Cleaners", "Market", "Pet Care", "Tire Center", "Florist", "Bistro"]
CI_TYPES = ["Manufacturing", "Distribution Center", "Cold Storage", "Data Center", "Medical Center",
            "Chemical", "Plastics", "Foods", "Packaging", "Steel", "Logistics", "Pharmaceuticals"]
BIZ_SUFFIX = ["LLC", "Inc", "Corp", "Co", ""]
GOV_TYPES = ["Township of {c}", "City of {c} Water Dept", "{c} Board of Education",
             "{c} Public Library", "{c} Police Dept", "{c} Housing Authority"]
