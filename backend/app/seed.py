"""Load repeatable, fictional ABC College demo records."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Assessment, AssessmentQuestion, AssessmentWorkspace, Student

STUDENTS = [
    ("2401", "Ananya Rao", "BSC1", "A"), ("2402", "Kabir Shah", "BSC1", "A"),
    ("2403", "Mira Iyer", "BSC1", "A"), ("2404", "Dev Patel", "BSC1", "A"),
    ("2405", "Sara Thomas", "BSC1", "A"), ("2411", "Ishaan Mehta", "BSC1", "B"),
    ("2412", "Nisha Kapoor", "BSC1", "B"), ("2413", "Rohan Das", "BSC1", "B"),
    ("2421", "Tara Joseph", "BSC2", "A"), ("2422", "Arjun Bose", "BSC2", "A"),
]
ASSESSMENTS = [
    ("bio-ut1", "Unit Test 1", "BIO101", "Introduction to Biology", "UT1", "Fall 2026", "18 Nov 2026", 20, "BSC1", "A"),
    ("bio-ut2", "Unit Test 2", "BIO101", "Introduction to Biology", "UT2", "Fall 2026", "22 Nov 2026", 20, "BSC1", "A"),
    ("chem-mid", "Midterm Examination", "CHEM101", "Chemistry Fundamentals", "MID", "Fall 2026", "12 Nov 2026", 50, "BSC1", "B"),
    ("eco-sem", "Semester 1 Examination", "ECO201", "Ecology", "SEM1", "Fall 2026", "08 Nov 2026", 100, "BSC2", "A"),
]
SCORES = {"bio-ut1": {"2401": 14, "2402": 16, "2403": 11, "2405": 17},
          "bio-ut2": {"2401": 16, "2402": 15, "2403": 12, "2405": 18},
          "chem-mid": {"2411": 34, "2413": 29}, "eco-sem": {"2421": 72, "2422": 81}}

SAMPLE_QUESTIONS = {
    "bio-ut1": [
        ("Q1", 1, "Explain how photosynthesis uses sunlight.", "Plants use light energy to convert carbon dioxide and water into glucose and oxygen.", 8,
         ["Names light energy", "Identifies carbon dioxide and water", "Explains glucose or oxygen as products"]),
        ("Q2", 2, "Describe one role of decomposers in an ecosystem.", "Decomposers break down dead matter and return nutrients to the environment.", 6,
         ["Identifies dead organic matter", "Describes breakdown", "Connects to nutrient recycling"]),
        ("Q3", 3, "Explain why energy transfer decreases between trophic levels.", "Organisms use much of the energy for life processes and release it as heat, so less energy is available to the next level.", 6,
         ["Identifies energy use in life processes", "Mentions energy lost as heat", "Connects to less energy at the next level"]),
    ],
    "bio-ut2": [
        ("Q1", 1, "Explain the role of cellular respiration.", "Cells release usable energy from glucose, usually using oxygen, and produce carbon dioxide and water.", 8,
         ["Names glucose", "Explains energy release", "Identifies major products"]),
        ("Q2", 2, "Describe how a food web differs from a food chain.", "A food chain shows one feeding path; a food web connects many feeding relationships in an ecosystem.", 6,
         ["Describes a single path", "Describes linked feeding relationships", "Uses ecosystem context"]),
        ("Q3", 3, "Give one example of how a population change affects another species.", "A decline in prey can reduce food for a predator and may lower the predator population.", 6,
         ["Names a change in one population", "Identifies an affected species", "Explains the relationship"]),
    ],
    "chem-mid": [
        ("Q1", 1, "Explain how a catalyst changes a chemical reaction.", "A catalyst provides a pathway with lower activation energy and is not consumed by the reaction.", 15,
         ["Describes a faster reaction", "Explains lower activation energy", "States that the catalyst is not consumed"]),
        ("Q2", 2, "Compare ionic and covalent bonding.", "Ionic bonding involves electron transfer and attraction between ions; covalent bonding involves shared electron pairs.", 15,
         ["Explains electron transfer", "Explains electron sharing", "Distinguishes resulting attractions"]),
        ("Q3", 3, "Use conservation of atoms to explain why chemical equations are balanced.", "Atoms are neither created nor destroyed in a chemical reaction, so each element must have the same atom count on both sides.", 20,
         ["States conservation of atoms", "Compares both sides", "Explains coefficient adjustments"]),
    ],
    "eco-sem": [
        ("Q1", 1, "Explain one way biodiversity can improve ecosystem resilience.", "Different species can perform overlapping roles, making ecosystem functions more likely to continue after a disturbance.", 25,
         ["Defines biodiversity or variety", "Explains overlapping roles", "Connects to recovery or stability"]),
        ("Q2", 2, "Describe how a predator decline can affect a food web.", "Prey may increase, which can affect plants or other predators and cause wider changes through the food web.", 25,
         ["Identifies prey response", "Explains effects on other organisms", "Shows linked food-web effects"]),
        ("Q3", 3, "Explain one cause and one consequence of habitat fragmentation.", "Roads or development can split habitat into isolated patches, limiting movement and reducing access to food or mates.", 25,
         ["Names a fragmentation cause", "Explains isolation", "Describes a consequence for organisms"]),
        ("Q4", 4, "Recommend a conservation action and explain why it could help.", "Protecting or reconnecting native habitat can support populations by preserving resources and movement routes.", 25,
         ["Proposes a relevant action", "Links it to habitat or resources", "Explains expected benefit"]),
    ],
}


def seed_demo_data(db: Session) -> None:
    """Insert sample records once; safe to call each time the API starts."""
    if db.scalar(select(Student.id).limit(1)) is not None:
        return
    students = {}
    for roll, name, class_code, section in STUDENTS:
        year = "Year 1" if class_code == "BSC1" else "Year 2"
        admission_year = "2026" if class_code == "BSC1" else "2025"
        student = Student(roll_number=roll, name=name, class_code=class_code, section=section,
            email=f"{name.lower().replace(' ', '.')}@example.test", program="B.Sc. Biology",
            academic_year="2026–27" if class_code == "BSC1" else "2025–26", year=year,
            admission_year=admission_year)
        db.add(student)
        students[roll] = student
    db.flush()
    for code, title, subject, subject_name, test, term, date, maximum, class_code, section in ASSESSMENTS:
        for roll, score in SCORES[code].items():
            db.add(Assessment(student=students[roll], assessment_code=code, title=title,
                subject_code=subject, subject_name=subject_name, test_code=test, term=term,
                date=date, score=score, max_score=maximum, review_status="Simulated demo result"))
    db.commit()


def seed_assessment_workspaces(db: Session) -> None:
    """Create reusable workspace records for the existing fictional demo tests."""
    for code, title, subject, _subject_name, test, term, date, _maximum, class_code, section in ASSESSMENTS:
        if db.get(AssessmentWorkspace, code) is None:
            db.add(AssessmentWorkspace(
                assessment_code=code, title=title, class_code=class_code, section=section,
                subject_code=subject, test_code=test, test_date=date, rules="",
                rubric_criteria=[],
            ))
        db.flush()
        if db.scalar(select(AssessmentQuestion.id).where(AssessmentQuestion.assessment_code == code).limit(1)) is None:
            for question_id, position, prompt, model_answer, maximum_marks, criteria in SAMPLE_QUESTIONS[code]:
                db.add(AssessmentQuestion(
                    assessment_code=code, question_id=question_id, position=position,
                    prompt=prompt, model_answer=model_answer, maximum_marks=maximum_marks,
                    rubric_criteria=criteria,
                ))
    db.commit()
