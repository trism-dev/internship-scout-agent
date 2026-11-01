"""Fictional sample resume and matches. Used by the /demo page and scripts/record_demo.py."""

RESUME = (
    "Sample Student (fictional). BS Computer Science, Example University, 2027. "
    "Python, SQL, FastAPI. Built a warehouse routing simulator. Intern project: robot arm "
    "telemetry dashboard. Interests: robotics, data pipelines."
)

# company, title, score, summary, reasons [(requirement, evidence)], gaps
SAMPLES = [
    (
        "Example Robotics",
        "Software Engineering Intern",
        92,
        "Robotics software role that matches the telemetry and Python experience.",
        [
            ("Python and data pipelines", "Telemetry dashboard project in Python"),
            ("Interest in robotics", "Warehouse routing simulator"),
        ],
        ["No ROS experience listed"],
    ),
    (
        "Sample Labs",
        "Data Engineering Intern",
        85,
        "Pipeline work that fits the SQL and FastAPI background.",
        [("SQL", "SQL listed in skills"), ("Backend services", "FastAPI listed in skills")],
        ["No cloud warehouse experience"],
    ),
    (
        "Placeholder Systems",
        "Backend Intern",
        78,
        "Backend role with a reasonable overlap.",
        [("Python backend", "FastAPI listed in skills")],
        ["No production deployment shown"],
    ),
    (
        "Demo Analytics",
        "Analytics Intern",
        66,
        "Some overlap in SQL; the role is more statistics than software.",
        [("SQL", "SQL listed in skills")],
        ["No statistics coursework listed"],
    ),
    (
        "Fictional Dynamics",
        "Embedded Software Intern",
        54,
        "Embedded focus; the resume shows little low-level work.",
        [("Interest in hardware", "Robot arm telemetry project")],
        ["No C or C++ listed"],
    ),
    (
        "Acme Sample Corp",
        "IT Support Intern",
        31,
        "Mostly support work, a weak fit.",
        [],
        ["No support experience listed"],
    ),
]
