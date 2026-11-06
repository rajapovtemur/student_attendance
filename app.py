import csv
import io
import re
import secrets
from datetime import date
from functools import wraps
from sqlalchemy import inspect, or_, func, text
from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, session, Response, flash, has_request_context
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from i18n import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, get_translation_dict

app = Flask(__name__)
app.config['SECRET_KEY'] = 'att-241-university-portal-secret-key-2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///att_university_portal.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

MAJOR_NAME = "Axborot tizimlari va texnologiyalari"
DEFAULT_GROUP = "ATT-241"

# Hemis Fixed Semester Constants: 36 classes per subject (18 Ma'ruza + 18 Amaliy/Laboratoriya)
HEMIS_TOTAL_CLASSES = 36
HEMIS_LECTURE_CLASSES = 18
HEMIS_PRACTICAL_CLASSES = 18

# Active QR tokens in memory: token -> { subject_id, class_type, date, teacher_id, ... }
ACTIVE_QR_SESSIONS = {}


# ==================== MULTILINGUAL (i18n) MIDDLEWARE & CONTEXT ==================== #

def get_current_lang() -> str:
    lang = request.args.get('lang') or session.get('lang') or DEFAULT_LANGUAGE
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE
    session['lang'] = lang
    return lang


@app.before_request
def handle_language_param():
    if 'lang' in request.args:
        lang_Param = request.args.get('lang')
        if lang_Param in SUPPORTED_LANGUAGES:
            session['lang'] = lang_Param


@app.context_processor
def inject_i18n():
    lang = session.get('lang', DEFAULT_LANGUAGE)
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE
    t_dict = get_translation_dict(lang)
    return {
        'current_lang': lang,
        'supported_languages': SUPPORTED_LANGUAGES,
        't': t_dict
    }


@app.route('/set-language/<lang_code>')
def set_language(lang_code):
    if lang_code in SUPPORTED_LANGUAGES:
        session['lang'] = lang_code
    ref = request.referrer
    return redirect(ref if ref else url_for('index'))


# ==================== SECURITY & HELPER FUNCTIONS ==================== #

def validate_password(password: str):
    """
    Validate password strength:
    - At least 8 characters long
    - Contains at least one letter (A-Z or a-z)
    - Contains at least one special symbol (e.g., @, #, $, !, %, etc.)
    """
    if not password or len(password) < 8:
        return False, "Parol kamida 8 ta belgidan iborat bo'lishi shart! (Password must be at least 8 characters long)"
    if not re.search(r'[A-Za-z]', password):
        return False, "Parolda kamida bitta harf (a-z, A-Z) qatnashishi shart! (Password must contain letters)"
    if not re.search(r'[^A-Za-z0-9\s]', password):
        return False, "Parolda kamida bitta maxsus belgi (@, #, $, !, % va h.k.) bo'lishi shart! (Password must contain special symbols)"
    return True, None


def format_teacher_username(full_name: str, provided_username: str = '') -> str:
    """
    Format teacher username using [lastname]_oqituvchi_davomat
    (e.g., 'Ramanov Muhiddin' -> 'ramanov_oqituvchi_davomat').
    """
    if provided_username and provided_username.endswith('_oqituvchi_davomat'):
        return provided_username.lower()

    parts = re.findall(r'[A-Za-z]+', full_name or '')
    lastname = parts[0].lower() if parts else 'ustoz'
    return f"{lastname}_oqituvchi_davomat"


# ==================== MANY-TO-MANY (N:M) ASSOCIATION TABLES ==================== #

# N:M: Teachers <-> Subjects (Teachers can teach multiple Subjects, Subjects can have multiple Teachers)
teacher_subjects = db.Table(
    'teacher_subjects',
    db.Column('teacher_id', db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    db.Column('subject_id', db.Integer, db.ForeignKey('subjects.id', ondelete='CASCADE'), primary_key=True)
)

# N:M: Subjects <-> Groups (Subjects belong to specific Groups, Groups study multiple Subjects)
subject_groups = db.Table(
    'subject_groups',
    db.Column('subject_id', db.Integer, db.ForeignKey('subjects.id', ondelete='CASCADE'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('groups.id', ondelete='CASCADE'), primary_key=True)
)


# ==================== DATABASE MODELS ==================== #

class Group(db.Model):
    __tablename__ = 'groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    major = db.Column(db.String(150), nullable=False, default=MAJOR_NAME)

    subjects = db.relationship(
        'Subject',
        secondary=subject_groups,
        back_populates='groups',
        lazy='subquery'
    )

    def to_dict(self):
        student_count = User.query.filter_by(role='student', group_name=self.name).count()
        return {
            'id': self.id,
            'name': self.name,
            'major': self.major,
            'student_count': student_count,
            'subject_ids': [s.id for s in self.subjects],
            'subject_names': [s.localized_name for s in self.subjects]
        }


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin', 'teacher', or 'student'
    group_name = db.Column(db.String(50), nullable=True, default=DEFAULT_GROUP)
    email = db.Column(db.String(120), unique=True, nullable=True)

    attendances = db.relationship(
        'Attendance',
        backref='student',
        lazy=True,
        cascade='all, delete-orphan'
    )

    # N:M relationship for Teachers <-> Subjects
    assigned_subjects = db.relationship(
        'Subject',
        secondary=teacher_subjects,
        back_populates='teachers',
        lazy='subquery'
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'full_name': self.full_name,
            'role': self.role,
            'group_name': self.group_name or (DEFAULT_GROUP if self.role == 'student' else None),
            'major': MAJOR_NAME,
            'email': self.email,
            'assigned_subject_ids': [s.id for s in self.assigned_subjects],
            'assigned_subject_names': [s.localized_name for s in self.assigned_subjects]
        }


class Subject(db.Model):
    """
    Single course record with 4-language dynamic translations (name_uz, name_uz_cyrl, name_ru, name_en),
    lecture_teacher, practical_teacher, and N:M relationships to Teachers (users) and Groups (groups).
    """
    __tablename__ = 'subjects'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(160), unique=True, nullable=False)
    name_uz = db.Column(db.String(160), nullable=False, default='')
    name_uz_cyrl = db.Column(db.String(160), nullable=False, default='')
    name_ru = db.Column(db.String(160), nullable=False, default='')
    name_en = db.Column(db.String(160), nullable=False, default='')
    lecture_teacher = db.Column(db.String(100), nullable=False)
    practical_teacher = db.Column(db.String(100), nullable=False)

    attendances = db.relationship(
        'Attendance',
        backref='subject',
        lazy=True,
        cascade='all, delete-orphan'
    )

    teachers = db.relationship(
        'User',
        secondary=teacher_subjects,
        back_populates='assigned_subjects',
        lazy='subquery'
    )

    groups = db.relationship(
        'Group',
        secondary=subject_groups,
        back_populates='subjects',
        lazy='subquery'
    )

    def get_localized_name(self, lang: str = None) -> str:
        """
        Helper method that checks the active language (from argument or Flask session)
        and returns the corresponding translated subject name.
        """
        if not lang:
            if has_request_context():
                lang = session.get('lang') or request.args.get('lang') or DEFAULT_LANGUAGE
            else:
                lang = DEFAULT_LANGUAGE

        lang_map = {
            'uz': self.name_uz,
            'uz_Cyrl': self.name_uz_cyrl,
            'ru': self.name_ru,
            'en': self.name_en,
        }
        translated = (lang_map.get(lang) or '').strip()
        if translated:
            return translated
        return (self.name_uz or self.name or '').strip()

    @property
    def localized_name(self) -> str:
        """
        Dynamic property returning the translated subject name for the active UI language.
        """
        return self.get_localized_name()

    def to_dict(self, current_teacher_name=None):
        allowed_types = ["Ma'ruza", "Amaliy"]
        if current_teacher_name:
            t_norm = current_teacher_name.strip().lower()
            types = []
            if self.lecture_teacher.strip().lower() == t_norm:
                types.append("Ma'ruza")
            if self.practical_teacher.strip().lower() == t_norm:
                types.append("Amaliy")
            if types:
                allowed_types = types

        loc_name = self.localized_name
        return {
            'id': self.id,
            'code': self.code,
            'name': loc_name,
            'localized_name': loc_name,
            'name_uz': self.name_uz or self.name,
            'name_uz_cyrl': self.name_uz_cyrl or self.name_uz or self.name,
            'name_ru': self.name_ru or self.name_uz or self.name,
            'name_en': self.name_en or self.name_uz or self.name,
            'lecture_teacher': self.lecture_teacher,
            'practical_teacher': self.practical_teacher,
            'allowed_class_types': allowed_types,
            'teacher_ids': [t.id for t in self.teachers],
            'teacher_names': [t.full_name for t in self.teachers],
            'group_ids': [g.id for g in self.groups],
            'group_names': [g.name for g in self.groups] or [DEFAULT_GROUP]
        }


class Attendance(db.Model):
    """
    Attendance linked to student, subject, and class_type ('Ma'ruza' or 'Amaliy').
    """
    __tablename__ = 'attendance'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    class_type = db.Column(db.String(20), nullable=False, default="Ma'ruza")  # 'Ma'ruza' or 'Amaliy'
    date = db.Column(db.Date, nullable=False, default=date.today)
    status = db.Column(db.String(20), nullable=False)  # 'present', 'absent', 'late'
    note = db.Column(db.String(255), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('student_id', 'subject_id', 'class_type', 'date', name='uq_student_subject_type_date'),
    )

    def to_dict(self):
        subj = self.subject
        teacher_name = 'N/A'
        if subj:
            teacher_name = subj.lecture_teacher if self.class_type == "Ma'ruza" else subj.practical_teacher

        loc_subj_name = subj.localized_name if subj else None
        return {
            'id': self.id,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'group_name': self.student.group_name if self.student else DEFAULT_GROUP,
            'subject_id': self.subject_id,
            'subject_name': loc_subj_name,
            'localized_subject_name': loc_subj_name,
            'subject_code': subj.code if subj else None,
            'class_type': self.class_type,
            'teacher_name': teacher_name,
            'date': self.date.isoformat(),
            'status': self.status,
            'note': self.note or ''
        }


class Schedule(db.Model):
    """
    Weekly Timetable (Dars Jadvali) model for ATT-241 group.
    """
    __tablename__ = 'schedules'

    id = db.Column(db.Integer, primary_key=True)
    day_of_week = db.Column(db.String(30), nullable=False)   # Dushanba, Seshanba, Chorshanba, Payshanba, Juma
    start_time = db.Column(db.String(15), nullable=False)    # e.g. '13:00'
    end_time = db.Column(db.String(15), nullable=False)      # e.g. '14:20'
    subject_name = db.Column(db.String(160), nullable=False)
    room = db.Column(db.String(30), nullable=False)          # e.g. 'B-208'
    class_type = db.Column(db.String(30), nullable=False)    # Ma'ruza / Amaliy / Laboratoriya
    teacher_name = db.Column(db.String(100), nullable=False)

    @property
    def localized_subject_name(self) -> str:
        return translate_schedule_subject_name(self.subject_name)

    def to_dict(self):
        loc_name = self.localized_subject_name
        return {
            'id': self.id,
            'day_of_week': self.day_of_week,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'time_range': f"{self.start_time}-{self.end_time}",
            'subject_name': loc_name,
            'localized_subject_name': loc_name,
            'room': self.room,
            'class_type': self.class_type,
            'teacher_name': self.teacher_name
        }


# ==================== SEED DATA DEFINITIONS ==================== #

EXACT_5_SUBJECTS = [
    {
        'code': 'ATT-101',
        'name_uz': 'Axborot tizimini ishlab chiqish metodologiyalari (ATIM)',
        'name_uz_cyrl': 'Ахборот тизимини ишлаб чиқиш методологиялари (АТИМ)',
        'name_ru': 'Методологии разработки информационных систем (ATIM)',
        'name_en': 'Information Systems Development Methodologies (ISDM)',
        'lecture_teacher': 'Ramanov Muhiddin',
        'practical_teacher': 'Ramanov Muhiddin',
    },
    {
        'code': 'ATT-102',
        'name_uz': 'Bulutli texnologiyalar',
        'name_uz_cyrl': 'Булутли технологиялар',
        'name_ru': 'Облачные технологии',
        'name_en': 'Cloud Technologies',
        'lecture_teacher': 'Allashkurov Dilshod',
        'practical_teacher': 'Ramanov Muhiddin',
    },
    {
        'code': 'ATT-103',
        'name_uz': 'Web tizimlar',
        'name_uz_cyrl': 'Веб тизимлар',
        'name_ru': 'Веб-системы',
        'name_en': 'Web Systems',
        'lecture_teacher': 'Raximbayev Musobek',
        'practical_teacher': 'Bazarbayev Murat',
    },
    {
        'code': 'ATT-104',
        'name_uz': 'Web texnologiyalar va dasturlash',
        'name_uz_cyrl': 'Веб технологиялар ва дастурлаш',
        'name_ru': 'Веб-технологии и программирование',
        'name_en': 'Web Technologies and Programming',
        'lecture_teacher': 'Raximbayev Musobek',
        'practical_teacher': 'Abdullayev Abdulla',
    },
    {
        'code': 'ATT-105',
        'name_uz': 'Operatsion tizimlar',
        'name_uz_cyrl': 'Операцион тизимлар',
        'name_ru': 'Операционные системы',
        'name_en': 'Operating Systems',
        'lecture_teacher': "O'qituvchi",
        'practical_teacher': "O'qituvchi",
    },
]

SCHEDULE_EXTRA_TRANSLATIONS = {
    'axborot tizimini ishlab chiqish metodologiyalari': 'ATT-101',
    'bulutli texnologiyalar': 'ATT-102',
    'veb tizimlari': 'ATT-103',
    'web tizimlar': 'ATT-103',
    'veb texnologiyalar va dasturlash': 'ATT-104',
    'web texnologiyalar va dasturlash': 'ATT-104',
    'operatsion tizimlar': 'ATT-105',
}

MARIFAT_TRANSLATIONS = {
    'uz': "Ma'rifat darsi",
    'uz_Cyrl': 'Маърифат дарси',
    'ru': 'Урок просвещения',
    'en': 'Enlightenment Lesson',
}


def translate_schedule_subject_name(raw_name: str) -> str:
    if not raw_name:
        return ''
    norm = raw_name.strip().lower()
    lang = get_current_lang() if has_request_context() else DEFAULT_LANGUAGE
    if norm == "ma'rifat darsi":
        return MARIFAT_TRANSLATIONS.get(lang, raw_name)

    matched_code = SCHEDULE_EXTRA_TRANSLATIONS.get(norm)
    subj = None
    if matched_code:
        subj = Subject.query.filter_by(code=matched_code).first()
    if not subj:
        subj = Subject.query.filter(
            or_(
                func.lower(Subject.name) == norm,
                func.lower(Subject.name_uz) == norm
            )
        ).first()
    if subj:
        return subj.get_localized_name(lang)
    return raw_name

# Cleaned Student List for ATT-241 (ONLY Last Name and First Name, excluding RO'ZIBOYEVA MARJONA)
CLEANED_ATT241_STUDENTS = [
    ('otaxonov_att241', 'OTAXONOV DILSHODBEK', 'otaxonov@att241.uz'),
    ('qutlimuratov_att241', 'QUTLIMURATOV ALISHER', 'qutlimuratov@att241.uz'),
    ('rajabov_eldor_att241', 'RAJABOV ELDOR', 'rajabov.e@att241.uz'),
    ('rajabov_temur_att241', 'RAJABOV TEMUR', 'rajabov.t@att241.uz'),
    ('safarboyev_att241', 'SAFARBOYEV ASADBEK', 'safarboyev@att241.uz'),
    ('sermaxanov_att241', 'SERMAXANOV RUSLAN', 'sermaxanov@att241.uz'),
    ('shirinov_att241', 'SHIRINOV SUHROBBEK', 'shirinov@att241.uz'),
    ('soatboyev_att241', 'SOATBOYEV ASADBEK', 'soatboyev@att241.uz'),
    ('xudoyberganov_att241', 'XUDOYBERGANOV SATIMBOY', 'xudoyberganov@att241.uz'),
]

# Exact Weekly Schedule for ATT-241
EXACT_ATT241_SCHEDULE = [
    # Dushanba
    ('Dushanba', '13:00', '14:20', 'Axborot tizimini ishlab chiqish metodologiyalari', 'B-208', "Ma'ruza", 'RAMANOV M. X.'),
    ('Dushanba', '14:30', '15:50', 'Bulutli texnologiyalar', 'A-202', "Ma'ruza", 'ALLASHKUROV D. M.'),
    ('Dushanba', '16:00', '17:20', 'Axborot tizimini ishlab chiqish metodologiyalari', 'B-202', 'Amaliy', 'RAMANOV M. X.'),
    # Seshanba
    ('Seshanba', '13:00', '14:20', 'Veb tizimlari', 'B-302', "Ma'ruza", 'RAXIMBAYEV M. K.'),
    ('Seshanba', '14:30', '15:50', 'Veb texnologiyalar va dasturlash', 'B-208', "Ma'ruza", 'RAXIMBAYEV M. K.'),
    ('Seshanba', '16:00', '17:20', 'Bulutli texnologiyalar', 'B-106', 'Laboratoriya', 'RAMANOV M. X.'),
    # Chorshanba
    ('Chorshanba', '13:00', '14:20', 'Operatsion tizimlar', 'B-202', "Ma'ruza", 'BAZARBAYEV M. X.'),
    ('Chorshanba', '14:30', '15:50', 'Operatsion tizimlar', 'B-206', 'Laboratoriya', 'ESHCHANOV T. S.'),
    # Payshanba
    ('Payshanba', '13:00', '14:20', 'Operatsion tizimlar', 'B-202', "Ma'ruza", 'BAZARBAYEV M. X.'),
    ('Payshanba', '14:30', '15:50', 'Veb tizimlari', 'B-206', 'Laboratoriya', 'BAZARBAYEV M. X.'),
    ('Payshanba', '16:00', '17:20', 'Bulutli texnologiyalar', 'A-202', "Ma'ruza", 'ALLASHKUROV D. M.'),
    # Juma
    ('Juma', '13:00', '14:20', "Ma'rifat darsi", 'B-305', "Ma'ruza", 'ATADJANOV K. S.'),
    ('Juma', '14:30', '15:50', 'Veb texnologiyalar va dasturlash', 'B-108', 'Laboratoriya', 'ABDULLAYEV A. F.'),
]


def sync_subject_nm_links(subject: Subject):
    """
    Synchronizes a Subject's N:M `teachers` relationship with its `lecture_teacher`
    and `practical_teacher` names, and ensures it is linked to at least the default group.
    """
    matched_teachers = User.query.filter(
        User.role == 'teacher',
        or_(
            func.lower(User.full_name) == subject.lecture_teacher.strip().lower(),
            func.lower(User.full_name) == subject.practical_teacher.strip().lower()
        )
    ).all()
    for t in matched_teachers:
        if t not in subject.teachers:
            subject.teachers.append(t)

    default_grp = Group.query.filter_by(name=DEFAULT_GROUP).first()
    if default_grp and default_grp not in subject.groups:
        subject.groups.append(default_grp)


def ensure_subject_translation_columns():
    """
    Ensures existing SQLite `subjects` table has `name_uz`, `name_uz_cyrl`, `name_ru`, and `name_en`
    columns without losing existing attendance or N:M records.
    """
    inspector = inspect(db.engine)
    if 'subjects' not in inspector.get_table_names():
        return
    existing_cols = {col['name'] for col in inspector.get_columns('subjects')}
    for col_name in ('name_uz', 'name_uz_cyrl', 'name_ru', 'name_en'):
        if col_name not in existing_cols:
            db.session.execute(
                text(f"ALTER TABLE subjects ADD COLUMN {col_name} VARCHAR(160) NOT NULL DEFAULT ''")
            )
    db.session.commit()


def initialize_and_seed_db():
    """
    Ensures all tables (including N:M `teacher_subjects`, `subject_groups`, and `groups`),
    Admin user, 6 Teachers (`123abc!`), 5 Subjects with 4-language translations,
    9 cleaned ATT-241 Students (`abc123!`), and Weekly Schedule are initialized and linked.
    """
    db.create_all()
    ensure_subject_translation_columns()

    # 0. Ensure Default Group ATT-241 exists
    att_group = Group.query.filter_by(name=DEFAULT_GROUP).first()
    if not att_group:
        att_group = Group(name=DEFAULT_GROUP, major=MAJOR_NAME)
        db.session.add(att_group)
        db.session.commit()

    # 0.5. Seed Default Admin Account (username: admin, password: admin123!)
    admin_user = User.query.filter_by(username='admin').first()
    if not admin_user:
        admin_user = User(
            username='admin',
            full_name='Tizim Administratori',
            role='admin',
            group_name=None,
            email='admin@university.uz'
        )
        admin_user.set_password('admin123!')
        db.session.add(admin_user)
        db.session.commit()

    # 1. Seed Teachers with [lastname]_oqituvchi_davomat and password '123abc!'
    teachers_seed = [
        ('ramanov_oqituvchi_davomat', 'Ramanov Muhiddin', 'ramanov@university.uz'),
        ('allashkurov_oqituvchi_davomat', 'Allashkurov Dilshod', 'allashkurov@university.uz'),
        ('raximbayev_oqituvchi_davomat', 'Raximbayev Musobek', 'raximbayev@university.uz'),
        ('bazarbayev_oqituvchi_davomat', 'Bazarbayev Murat', 'bazarbayev@university.uz'),
        ('abdullayev_oqituvchi_davomat', 'Abdullayev Abdulla', 'abdullayev@university.uz'),
        ('oqituvchi_oqituvchi_davomat', "O'qituvchi", 'oqituvchi@university.uz'),
    ]

    for uname, fname, email in teachers_seed:
        t = User.query.filter_by(username=uname).first()
        if not t:
            t_by_email = User.query.filter_by(email=email).first()
            if t_by_email:
                t_by_email.username = uname
                t_by_email.full_name = fname
                t_by_email.set_password('123abc!')
            else:
                t = User(username=uname, full_name=fname, role='teacher', group_name=None, email=email)
                t.set_password('123abc!')
                db.session.add(t)
    db.session.commit()

    # 2. Seed or update the 5 subjects with 4-language translations and sync N:M relationships
    for item in EXACT_5_SUBJECTS:
        s = Subject.query.filter_by(code=item['code']).first()
        if not s:
            s = Subject.query.filter_by(name=item['name_uz']).first()
        if not s:
            s = Subject(
                code=item['code'],
                name=item['name_uz'],
                name_uz=item['name_uz'],
                name_uz_cyrl=item['name_uz_cyrl'],
                name_ru=item['name_ru'],
                name_en=item['name_en'],
                lecture_teacher=item['lecture_teacher'],
                practical_teacher=item['practical_teacher']
            )
            db.session.add(s)
        else:
            s.name = s.name or item['name_uz']
            s.name_uz = s.name_uz or item['name_uz']
            s.name_uz_cyrl = s.name_uz_cyrl or item['name_uz_cyrl']
            s.name_ru = s.name_ru or item['name_ru']
            s.name_en = s.name_en or item['name_en']
    db.session.commit()

    for s in Subject.query.all():
        if not s.name_uz:
            s.name_uz = s.name
        if not s.name_uz_cyrl:
            s.name_uz_cyrl = s.name_uz
        if not s.name_ru:
            s.name_ru = s.name_uz
        if not s.name_en:
            s.name_en = s.name_uz
        sync_subject_nm_links(s)
    db.session.commit()

    # 3. Ensure cleaned ATT-241 students exist and RO'ZIBOYEVA MARJONA is excluded
    marjona = User.query.filter(func.upper(User.full_name).like("%RO'ZIBOYEVA%")).all()
    for m in marjona:
        db.session.delete(m)
    db.session.commit()

    if User.query.filter_by(role='student').count() == 0:
        for uname, fname, email in CLEANED_ATT241_STUDENTS:
            st = User(
                username=uname,
                full_name=fname,
                role='student',
                group_name=DEFAULT_GROUP,
                email=email
            )
            st.set_password('abc123!')
            db.session.add(st)
        db.session.commit()

    # 4. Seed the exact Weekly Schedule (Dars Jadvali) for ATT-241
    if Schedule.query.count() != len(EXACT_ATT241_SCHEDULE):
        Schedule.query.delete()
        for day, start_t, end_t, s_name, room, c_type, t_name in EXACT_ATT241_SCHEDULE:
            db.session.add(Schedule(
                day_of_week=day,
                start_time=start_t,
                end_time=end_t,
                subject_name=s_name,
                room=room,
                class_type=c_type,
                teacher_name=t_name
            ))
        db.session.commit()


with app.app_context():
    initialize_and_seed_db()


# ==================== AUTH DECORATORS & HEMIS ATTENDANCE ALGORITHM ==================== #

def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session:
                if request.path.startswith('/api/'):
                    return jsonify({'error': 'Unauthorized'}), 401
                return redirect(url_for('login_page'))
            if role:
                allowed_roles = role if isinstance(role, (list, tuple, set)) else (role, 'admin')
                if session.get('role') not in allowed_roles:
                    if request.path.startswith('/api/'):
                        return jsonify({'error': 'Forbidden'}), 403
                    return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def get_teacher_subjects(teacher_full_name: str, user_id: int = None):
    """
    Returns subjects assigned to the teacher via EITHER the N:M `teacher_subjects`
    relationship OR matching `lecture_teacher` / `practical_teacher` names.
    """
    matched_ids = set()
    if user_id:
        u = User.query.get(user_id)
        if u and u.assigned_subjects:
            for s in u.assigned_subjects:
                matched_ids.add(s.id)

    if teacher_full_name:
        t_norm = teacher_full_name.strip().lower()
        by_name = Subject.query.filter(
            or_(
                func.lower(Subject.lecture_teacher) == t_norm,
                func.lower(Subject.practical_teacher) == t_norm
            )
        ).all()
        for s in by_name:
            matched_ids.add(s.id)

    if not matched_ids:
        return []
    return Subject.query.filter(Subject.id.in_(matched_ids)).order_by(Subject.id.asc()).all()


def calculate_student_stats(student_id, subject_id=None):
    """
    Official 'Hemis' Fixed Attendance Algorithm:
    - Fixed semester total per subject = 36 classes (18 Ma'ruza + 18 Amaliy/Laboratoriya).
    - Absence Rate = (absent_count / 36) * 100
      * 0 absences = 0%
      * 1 absence  = (1 / 36) * 100 = 2.78%
      * 2 absences = (2 / 36) * 100 = 5.56%
      * 9 absences = (9 / 36) * 100 = 25.0%
      * 10+ absences = 27.78%+ -> Strictly exceeds 25% -> RED LINE (Fail Warning)
    """
    query = Attendance.query.filter_by(student_id=student_id)
    if subject_id:
        query = query.filter_by(subject_id=subject_id)

    records = query.order_by(Attendance.date.desc(), Attendance.id.desc()).all()

    absent_records = [r for r in records if r.status == 'absent']
    absent_count = len(absent_records)
    present_marked = sum(1 for r in records if r.status in ('present', 'late'))

    lecture_absent = sum(1 for r in absent_records if r.class_type == "Ma'ruza")
    practical_absent = sum(1 for r in absent_records if r.class_type in ("Amaliy", "Laboratoriya"))

    # Hemis fixed 36-class calculation: (absent_count / 36) * 100
    missed_pct = round((absent_count / HEMIS_TOTAL_CLASSES) * 100, 2)
    if missed_pct.is_integer():
        missed_pct = int(missed_pct) if missed_pct == 0 else missed_pct

    remaining_classes = max(0, HEMIS_TOTAL_CLASSES - absent_count)
    attendance_pct = round(max(0.0, 100.0 - float(missed_pct)), 2)

    # Fail Condition (Red Line): strictly exceeds 25% (i.e. >= 10 missed classes out of 36)
    is_red_line = float(missed_pct) > 25.0

    # Condition Arrow trend based on recent attendance records
    recent = records[:3]
    if not recent:
        trend = 'up'
        trend_label = "Qoldirilgan dars yo'q (0%)"
    elif recent[0].status == 'absent':
        trend = 'down'
        trend_label = f"Oxirgi darsda qatnashmagan ({recent[0].date.isoformat()} - {recent[0].class_type})"
    elif sum(1 for r in recent if r.status == 'absent') >= 2:
        trend = 'down'
        trend_label = "Oxirgi darslarda qoldirishlar mavjud"
    else:
        trend = 'up'
        trend_label = "Oxirgi darslarda qatnashgan"

    return {
        'fixed_semester_total': HEMIS_TOTAL_CLASSES,
        'lecture_max': HEMIS_LECTURE_CLASSES,
        'practical_max': HEMIS_PRACTICAL_CLASSES,
        'lecture_absent': lecture_absent,
        'practical_absent': practical_absent,
        'absent_count': absent_count,
        'remaining_classes': remaining_classes,
        'present_marked': present_marked,
        'missed_percentage': missed_pct,
        'attendance_percentage': attendance_pct,
        'is_red_line': is_red_line,
        'trend': trend,
        'trend_label': trend_label,
        'missed_records': [r.to_dict() for r in absent_records]
    }


def get_grouped_weekly_schedule():
    """
    Returns the ATT-241 weekly schedule grouped by day_of_week in chronological order.
    """
    days_order = ['Dushanba', 'Seshanba', 'Chorshanba', 'Payshanba', 'Juma']
    all_items = Schedule.query.order_by(Schedule.id.asc()).all()
    grouped = []
    for day in days_order:
        day_classes = [item.to_dict() for item in all_items if item.day_of_week == day]
        if day_classes:
            grouped.append({
                'day': day,
                'classes': day_classes
            })
    return grouped


# ==================== AUTHENTICATION ROUTES ==================== #

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    role = session.get('role')
    if role == 'admin':
        return redirect(url_for('admin_dashboard'))
    if role == 'teacher':
        return redirect(url_for('teacher_dashboard'))
    return redirect(url_for('student_dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'GET':
        if 'user_id' in session:
            return redirect(url_for('index'))
        return render_template('login.html')

    data = request.get_json() if request.is_json else request.form
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    user = User.query.filter_by(username=username).first()
    # Security UX: Whether username doesn't exist OR password is wrong, return ONE generic message:
    # "Login yoki parol xato"
    if not user or not user.check_password(password):
        t_dict = get_translation_dict(get_current_lang())
        generic_error = t_dict.get('login_error', 'Login yoki parol xato')
        flash(generic_error, 'danger')
        if request.is_json:
            return jsonify({'error': generic_error}), 401
        return render_template('login.html', error=generic_error, username=username)

    session['user_id'] = user.id
    session['username'] = user.username
    session['full_name'] = user.full_name
    session['role'] = user.role
    session['group_name'] = user.group_name or DEFAULT_GROUP

    if user.role == 'admin':
        redirect_url = url_for('admin_dashboard')
    elif user.role == 'teacher':
        redirect_url = url_for('teacher_dashboard')
    else:
        redirect_url = url_for('student_dashboard')

    if request.is_json:
        return jsonify({'message': 'Muvaffaqiyatli kirdingiz!', 'redirect': redirect_url, 'role': user.role}), 200
    return redirect(redirect_url)


@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'GET':
        if 'user_id' in session:
            return redirect(url_for('index'))
        return render_template('register.html', default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    data = request.get_json() if request.is_json else request.form
    full_name = (data.get('full_name') or '').strip()
    raw_username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    role = (data.get('role') or 'student').strip().lower()
    group_name = (data.get('group_name') or DEFAULT_GROUP).strip() or DEFAULT_GROUP
    email = (data.get('email') or '').strip() or None

    if not full_name or not password:
        err = "Barcha majburiy maydonlarni (F.I.Sh, Username, Parol) to'ldiring!"
        return (jsonify({'error': err}), 400) if request.is_json else render_template('register.html', error=err, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    if role not in ('teacher', 'student'):
        err = "Rol faqat 'teacher' yoki 'student' bo'lishi kerak!"
        return (jsonify({'error': err}), 400) if request.is_json else render_template('register.html', error=err, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    if role == 'teacher':
        username = format_teacher_username(full_name, raw_username)
    else:
        username = raw_username

    if not username:
        err = "Username kiritilishi shart!"
        return (jsonify({'error': err}), 400) if request.is_json else render_template('register.html', error=err, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    is_valid_pw, pw_error = validate_password(password)
    if not is_valid_pw:
        return (jsonify({'error': pw_error}), 400) if request.is_json else render_template('register.html', error=pw_error, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    if User.query.filter_by(username=username).first():
        err = f"'{username}' username allaqachon band! Boshqa username tanlang yoki login qiling."
        return (jsonify({'error': err}), 400) if request.is_json else render_template('register.html', error=err, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    if email and User.query.filter_by(email=email).first():
        err = "Ushbu email manzili allaqachon ro'yxatdan o'tgan!"
        return (jsonify({'error': err}), 400) if request.is_json else render_template('register.html', error=err, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    new_user = User(
        username=username,
        full_name=full_name,
        role=role,
        group_name=group_name if role == 'student' else None,
        email=email
    )
    new_user.set_password(password)

    try:
        db.session.add(new_user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        err = "Ro'yxatdan o'tishda xatolik yuz berdi!"
        return (jsonify({'error': err}), 400) if request.is_json else render_template('register.html', error=err, default_group=DEFAULT_GROUP, major_name=MAJOR_NAME)

    session['user_id'] = new_user.id
    session['username'] = new_user.username
    session['full_name'] = new_user.full_name
    session['role'] = new_user.role
    session['group_name'] = new_user.group_name or DEFAULT_GROUP

    redirect_url = url_for('teacher_dashboard') if new_user.role == 'teacher' else url_for('student_dashboard')
    if request.is_json:
        return jsonify({
            'message': f"Hisob muvaffaqiyatli yaratildi! (Username: {new_user.username})",
            'redirect': redirect_url,
            'role': new_user.role,
            'username': new_user.username
        }), 201
    return redirect(redirect_url)


@app.route('/logout')
def logout():
    lang = session.get('lang', DEFAULT_LANGUAGE)
    session.clear()
    session['lang'] = lang
    return redirect(url_for('login_page'))


@app.route('/teacher')
@login_required(role=('teacher', 'admin'))
def teacher_dashboard():
    subjects = get_teacher_subjects(session.get('full_name', ''), session.get('user_id'))
    if not subjects and session.get('role') == 'admin':
        subjects = Subject.query.order_by(Subject.id.asc()).all()
    return render_template('teacher_dashboard.html', user=session, subjects=subjects, major_name=MAJOR_NAME, default_group=DEFAULT_GROUP)


@app.route('/student')
@login_required(role=('student', 'admin'))
def student_dashboard():
    active_tab = request.args.get('tab', 'dashboard')
    subjects = Subject.query.order_by(Subject.id.asc()).all()
    return render_template('student_dashboard.html', user=session, subjects=subjects, major_name=MAJOR_NAME, default_group=DEFAULT_GROUP, active_tab=active_tab)


@app.route('/admin')
@login_required(role=('admin', 'teacher'))
def admin_dashboard():
    subjects = Subject.query.order_by(Subject.id.asc()).all()
    return render_template('admin_dashboard.html', user=session, subjects=subjects, major_name=MAJOR_NAME, default_group=DEFAULT_GROUP)


@app.route('/settings')
@app.route('/profile')
@login_required()
def profile_settings():
    current_user = User.query.get_or_404(session['user_id'])
    return render_template(
        'profile_settings.html',
        user=current_user.to_dict(),
        major_name=MAJOR_NAME,
        default_group=DEFAULT_GROUP
    )


@app.route('/api/profile/update-info', methods=['POST'])
@login_required()
def api_update_profile_info():
    user = User.query.get_or_404(session['user_id'])
    t_dict = get_translation_dict(get_current_lang())
    data = request.get_json() if request.is_json else request.form

    full_name = (data.get('full_name') or '').strip()
    username = (data.get('username') or '').strip()
    email = (data.get('email') or '').strip() or None

    if not full_name or not username:
        return jsonify({'error': "F.I.Sh va Username bo'sh bo'lishi mumkin emas!"}), 400

    existing_u = User.query.filter(User.username == username, User.id != user.id).first()
    if existing_u:
        return jsonify({'error': f"'{username}' allaqachon band!"}), 400

    if email:
        existing_e = User.query.filter(User.email == email, User.id != user.id).first()
        if existing_e:
            return jsonify({'error': "Ushbu email boshqa foydalanuvchi tomonidan band qilingan!"}), 400

    old_full_name = user.full_name
    if user.role == 'student':
        full_name = full_name.upper()

    user.full_name = full_name
    user.username = username
    user.email = email

    # Keep Subject teacher name columns synchronized if a teacher updates their full_name
    if user.role == 'teacher' and old_full_name and old_full_name != full_name:
        for subj in Subject.query.all():
            if subj.lecture_teacher.strip().lower() == old_full_name.strip().lower():
                subj.lecture_teacher = full_name
            if subj.practical_teacher.strip().lower() == old_full_name.strip().lower():
                subj.practical_teacher = full_name

    db.session.commit()

    session['full_name'] = user.full_name
    session['username'] = user.username

    return jsonify({
        'message': t_dict.get('msg_profile_updated', "Shaxsiy ma'lumotlaringiz muvaffaqiyatli yangilandi!"),
        'user': user.to_dict()
    }), 200


@app.route('/api/profile/change-password', methods=['POST'])
@login_required()
def api_change_profile_password():
    user = User.query.get_or_404(session['user_id'])
    t_dict = get_translation_dict(get_current_lang())
    data = request.get_json() if request.is_json else request.form

    current_password = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    confirm_password = data.get('confirm_password') or ''

    if not user.check_password(current_password):
        return jsonify({'error': t_dict.get('err_wrong_current_pw', "Joriy parol noto'g'ri kiritildi!")}), 400

    if new_password != confirm_password:
        return jsonify({'error': t_dict.get('err_pw_mismatch', "Yangi parol va tasdiqlash paroli mos kelmadi!")}), 400

    is_valid, pw_err = validate_password(new_password)
    if not is_valid:
        return jsonify({'error': pw_err}), 400

    user.set_password(new_password)
    db.session.commit()

    return jsonify({
        'message': t_dict.get('msg_password_updated', "Parolingiz muvaffaqiyatli o'zgartirildi!")
    }), 200


# ==================== ADMIN PANEL & N:M CRUD API ==================== #

@app.route('/api/admin/overview', methods=['GET'])
@login_required(role=('admin', 'teacher'))
def admin_overview():
    teachers = User.query.filter_by(role='teacher').order_by(User.full_name.asc()).all()
    students = User.query.filter_by(role='student').order_by(User.full_name.asc()).all()
    subjects = Subject.query.order_by(Subject.id.asc()).all()
    groups = Group.query.order_by(Group.name.asc()).all()

    student_list = []
    for s in students:
        d = s.to_dict()
        d['overall_stats'] = calculate_student_stats(s.id)
        student_list.append(d)

    return jsonify({
        'teachers': [t.to_dict() for t in teachers],
        'students': student_list,
        'subjects': [subj.to_dict() for subj in subjects],
        'groups': [g.to_dict() for g in groups]
    }), 200


@app.route('/api/admin/teachers', methods=['POST'])
@login_required(role=('admin', 'teacher'))
def admin_create_teacher():
    data = request.get_json()
    full_name = (data.get('full_name') or '').strip()
    raw_username = (data.get('username') or '').strip()
    password = (data.get('password') or '123abc!').strip() or '123abc!'
    email = (data.get('email') or '').strip() or None
    subject_ids = data.get('subject_ids', [])

    if not full_name:
        return jsonify({'error': "O'qituvchi F.I.Sh kiritilishi shart!"}), 400

    username = format_teacher_username(full_name, raw_username) if not raw_username else raw_username
    if User.query.filter_by(username=username).first():
        return jsonify({'error': f"'{username}' username allaqachon mavjud!"}), 400

    teacher = User(
        username=username,
        full_name=full_name,
        role='teacher',
        group_name=None,
        email=email
    )
    teacher.set_password(password)

    if subject_ids:
        subjs = Subject.query.filter(Subject.id.in_(subject_ids)).all()
        teacher.assigned_subjects = subjs

    db.session.add(teacher)
    db.session.commit()
    return jsonify({'message': "O'qituvchi muvaffaqiyatli qo'shildi!", 'teacher': teacher.to_dict()}), 201


@app.route('/api/admin/teachers/<int:user_id>', methods=['PUT', 'DELETE'])
@login_required(role=('admin', 'teacher'))
def admin_modify_teacher(user_id):
    teacher = User.query.filter_by(id=user_id, role='teacher').first_or_404()

    if request.method == 'DELETE':
        db.session.delete(teacher)
        db.session.commit()
        return jsonify({'message': "O'qituvchi tizimdan o'chirildi!"}), 200

    data = request.get_json()
    full_name = (data.get('full_name') or '').strip()
    username = (data.get('username') or '').strip()
    email = (data.get('email') or '').strip() or None
    subject_ids = data.get('subject_ids')

    if not full_name or not username:
        return jsonify({'error': "F.I.Sh va Username bo'sh bo'lishi mumkin emas!"}), 400

    existing = User.query.filter(User.username == username, User.id != teacher.id).first()
    if existing:
        return jsonify({'error': f"'{username}' boshqa foydalanuvchi tomonidan band qilingan!"}), 400

    teacher.full_name = full_name
    teacher.username = username
    teacher.email = email

    if isinstance(subject_ids, list):
        subjs = Subject.query.filter(Subject.id.in_(subject_ids)).all()
        teacher.assigned_subjects = subjs

    db.session.commit()
    return jsonify({'message': "O'qituvchi ma'lumotlari yangilandi!", 'teacher': teacher.to_dict()}), 200


@app.route('/api/admin/students', methods=['POST'])
@login_required(role=('admin', 'teacher'))
def admin_create_student():
    data = request.get_json()
    full_name = (data.get('full_name') or '').strip().upper()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or 'abc123!').strip() or 'abc123!'
    group_name = (data.get('group_name') or DEFAULT_GROUP).strip() or DEFAULT_GROUP
    email = (data.get('email') or '').strip() or None

    if not full_name or not username:
        return jsonify({'error': "F.I.Sh va Username kiritilishi shart!"}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': f"'{username}' username allaqachon band!"}), 400

    # Ensure group exists in Group table
    if not Group.query.filter_by(name=group_name).first():
        db.session.add(Group(name=group_name, major=MAJOR_NAME))

    student = User(
        username=username,
        full_name=full_name,
        role='student',
        group_name=group_name,
        email=email
    )
    student.set_password(password)
    db.session.add(student)
    db.session.commit()
    return jsonify({'message': "Talaba muvaffaqiyatli qo'shildi!", 'student': student.to_dict()}), 201


@app.route('/api/admin/students/<int:user_id>', methods=['PUT', 'DELETE'])
@login_required(role=('admin', 'teacher'))
def admin_modify_student(user_id):
    student = User.query.filter_by(id=user_id, role='student').first_or_404()

    if request.method == 'DELETE':
        db.session.delete(student)
        db.session.commit()
        return jsonify({'message': "Talaba o'chirildi!"}), 200

    data = request.get_json()
    full_name = (data.get('full_name') or '').strip().upper()
    username = (data.get('username') or '').strip()
    group_name = (data.get('group_name') or DEFAULT_GROUP).strip() or DEFAULT_GROUP
    email = (data.get('email') or '').strip() or None

    if not full_name or not username:
        return jsonify({'error': "F.I.Sh va Username kiritilishi shart!"}), 400

    existing = User.query.filter(User.username == username, User.id != student.id).first()
    if existing:
        return jsonify({'error': f"'{username}' username band!"}), 400

    if not Group.query.filter_by(name=group_name).first():
        db.session.add(Group(name=group_name, major=MAJOR_NAME))

    student.full_name = full_name
    student.username = username
    student.group_name = group_name
    student.email = email
    db.session.commit()
    return jsonify({'message': "Talaba ma'lumotlari yangilandi!", 'student': student.to_dict()}), 200


@app.route('/api/admin/subjects', methods=['POST'])
@login_required(role=('admin', 'teacher'))
def admin_create_subject():
    data = request.get_json()
    code = (data.get('code') or '').strip().upper()
    name_uz = (data.get('name_uz') or data.get('name') or '').strip()
    name_uz_cyrl = (data.get('name_uz_cyrl') or name_uz).strip()
    name_ru = (data.get('name_ru') or name_uz).strip()
    name_en = (data.get('name_en') or name_uz).strip()
    lecture_teacher = (data.get('lecture_teacher') or '').strip()
    practical_teacher = (data.get('practical_teacher') or '').strip()
    teacher_ids = data.get('teacher_ids', [])
    group_ids = data.get('group_ids', [])

    if not code or not name_uz or not lecture_teacher or not practical_teacher:
        return jsonify({'error': "Fan kodi, nomi, Ma'ruza va Amaliy o'qituvchilar kiritilishi shart!"}), 400

    if Subject.query.filter(or_(Subject.code == code, Subject.name == name_uz)).first():
        return jsonify({'error': "Bunday fan kodi yoki fan nomi allaqachon mavjud!"}), 400

    subj = Subject(
        code=code,
        name=name_uz,
        name_uz=name_uz,
        name_uz_cyrl=name_uz_cyrl,
        name_ru=name_ru,
        name_en=name_en,
        lecture_teacher=lecture_teacher,
        practical_teacher=practical_teacher
    )
    if teacher_ids:
        subj.teachers = User.query.filter(User.id.in_(teacher_ids), User.role == 'teacher').all()
    if group_ids:
        subj.groups = Group.query.filter(Group.id.in_(group_ids)).all()

    db.session.add(subj)
    db.session.flush()
    sync_subject_nm_links(subj)
    db.session.commit()
    return jsonify({'message': "Yangi fan muvaffaqiyatli qo'shildi!", 'subject': subj.to_dict()}), 201


@app.route('/api/admin/subjects/<int:subject_id>', methods=['PUT', 'DELETE'])
@login_required(role=('admin', 'teacher'))
def admin_modify_subject(subject_id):
    subj = Subject.query.get_or_404(subject_id)

    if request.method == 'DELETE':
        db.session.delete(subj)
        db.session.commit()
        return jsonify({'message': "Fan tizimdan o'chirildi!"}), 200

    data = request.get_json()
    code = (data.get('code') or '').strip().upper()
    name_uz = (data.get('name_uz') or data.get('name') or '').strip()
    name_uz_cyrl = (data.get('name_uz_cyrl') or name_uz).strip()
    name_ru = (data.get('name_ru') or name_uz).strip()
    name_en = (data.get('name_en') or name_uz).strip()
    lecture_teacher = (data.get('lecture_teacher') or '').strip()
    practical_teacher = (data.get('practical_teacher') or '').strip()
    teacher_ids = data.get('teacher_ids')
    group_ids = data.get('group_ids')

    if not code or not name_uz or not lecture_teacher or not practical_teacher:
        return jsonify({'error': "Barcha fan maydonlari to'ldirilishi shart!"}), 400

    subj.code = code
    subj.name = name_uz
    subj.name_uz = name_uz
    subj.name_uz_cyrl = name_uz_cyrl
    subj.name_ru = name_ru
    subj.name_en = name_en
    subj.lecture_teacher = lecture_teacher
    subj.practical_teacher = practical_teacher

    if isinstance(teacher_ids, list):
        subj.teachers = User.query.filter(User.id.in_(teacher_ids), User.role == 'teacher').all()
    if isinstance(group_ids, list):
        subj.groups = Group.query.filter(Group.id.in_(group_ids)).all()

    sync_subject_nm_links(subj)
    db.session.commit()
    return jsonify({'message': "Fan ma'lumotlari muvaffaqiyatli yangilandi!", 'subject': subj.to_dict()}), 200


@app.route('/api/admin/groups', methods=['POST'])
@login_required(role=('admin', 'teacher'))
def admin_create_group():
    data = request.get_json()
    name = (data.get('name') or '').strip().upper()
    major = (data.get('major') or MAJOR_NAME).strip() or MAJOR_NAME
    subject_ids = data.get('subject_ids', [])

    if not name:
        return jsonify({'error': "Guruh nomi kiritilishi shart!"}), 400

    if Group.query.filter_by(name=name).first():
        return jsonify({'error': f"'{name}' guruhi allaqachon mavjud!"}), 400

    grp = Group(name=name, major=major)
    if subject_ids:
        grp.subjects = Subject.query.filter(Subject.id.in_(subject_ids)).all()
    db.session.add(grp)
    db.session.commit()
    return jsonify({'message': f"'{name}' guruhi yaratildi!", 'group': grp.to_dict()}), 201


@app.route('/api/admin/groups/<int:group_id>', methods=['DELETE'])
@login_required(role=('admin', 'teacher'))
def admin_delete_group(group_id):
    grp = Group.query.get_or_404(group_id)
    if grp.name == DEFAULT_GROUP:
        return jsonify({'error': "Asosiy ATT-241 guruhini o'chirib bo'lmaydi!"}), 400
    db.session.delete(grp)
    db.session.commit()
    return jsonify({'message': "Guruh o'chirildi!"}), 200


@app.route('/api/admin/users/<int:user_id>/reset-password', methods=['POST'])
@login_required(role=('admin', 'teacher'))
def admin_reset_user_password(user_id):
    """
    Allows Admin to reset the password of any Student or Teacher.
    """
    user = User.query.get_or_404(user_id)
    data = request.get_json()
    new_password = (data.get('new_password') or '').strip()

    if not new_password or len(new_password) < 4:
        return jsonify({'error': "Yangi parol kamida 4 ta belgidan iborat bo'lishi kerak!"}), 400

    user.set_password(new_password)
    db.session.commit()
    return jsonify({
        'message': f"'{user.full_name}' (@{user.username}) uchun yangi parol muvaffaqiyatli o'rnatildi!"
    }), 200


# ==================== TEACHER & GENERAL API ==================== #

@app.route('/api/subjects', methods=['GET'])
@login_required()
def get_subjects():
    if session.get('role') == 'teacher':
        teacher_name = session.get('full_name', '')
        matched = get_teacher_subjects(teacher_name, session.get('user_id'))
        return jsonify([s.to_dict(current_teacher_name=teacher_name) for s in matched]), 200

    subjects = Subject.query.order_by(Subject.id.asc()).all()
    return jsonify([s.to_dict() for s in subjects]), 200


@app.route('/api/schedule', methods=['GET'])
@login_required()
def get_schedule():
    return jsonify(get_grouped_weekly_schedule()), 200


@app.route('/api/students', methods=['GET', 'POST'])
@login_required(role=('teacher', 'admin'))
def handle_students():
    if request.method == 'GET':
        subject_id = request.args.get('subject_id', type=int)
        students = User.query.filter_by(role='student').order_by(User.full_name.asc()).all()
        result = []
        for s in students:
            d = s.to_dict()
            overall_stats = calculate_student_stats(s.id)
            subj_stats = calculate_student_stats(s.id, subject_id) if subject_id else overall_stats
            d['overall_stats'] = overall_stats
            d['subject_stats'] = subj_stats
            d['is_red_line'] = subj_stats['is_red_line'] or overall_stats['is_red_line']
            result.append(d)
        return jsonify(result), 200

    data = request.get_json()
    full_name = (data.get('full_name') or '').strip().upper()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or 'abc123!').strip()
    group_name = (data.get('group_name') or DEFAULT_GROUP).strip() or DEFAULT_GROUP
    email = (data.get('email') or '').strip() or None

    if not full_name or not username or not password:
        return jsonify({'error': "F.I.Sh, Username va Parol kiritilishi shart!"}), 400

    if "RO'ZIBOYEVA MARJONA" in full_name:
        return jsonify({'error': "Ushbu talaba ro'yxatdan chiqarilgan!"}), 400

    if password != 'abc123!':
        is_valid_pw, pw_error = validate_password(password)
        if not is_valid_pw:
            return jsonify({'error': pw_error}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({'error': "Ushbu username band!"}), 400

    new_student = User(
        username=username,
        full_name=full_name,
        role='student',
        group_name=group_name,
        email=email
    )
    new_student.set_password(password)
    try:
        db.session.add(new_student)
        db.session.commit()
        return jsonify({'message': "Talaba ATT-241 guruhiga muvaffaqiyatli qo'shildi!", 'student': new_student.to_dict()}), 201
    except Exception:
        db.session.rollback()
        return jsonify({'error': "Email band yoki xatolik yuz berdi"}), 400


@app.route('/api/students/<int:student_id>', methods=['GET', 'DELETE'])
@login_required(role=('teacher', 'admin'))
def student_detail(student_id):
    student = User.query.filter_by(id=student_id, role='student').first_or_404()
    if request.method == 'DELETE':
        db.session.delete(student)
        db.session.commit()
        return jsonify({'message': "Talaba o'chirildi!"}), 200

    subject_id = request.args.get('subject_id', type=int)
    d = student.to_dict()
    d['stats'] = calculate_student_stats(student.id, subject_id)
    return jsonify(d), 200


@app.route('/api/attendance', methods=['GET'])
@login_required(role=('teacher', 'admin'))
def get_attendance():
    subject_id = request.args.get('subject_id', type=int)
    class_type = request.args.get('class_type')
    date_str = request.args.get('date')

    query = Attendance.query
    if subject_id:
        query = query.filter_by(subject_id=subject_id)
    if class_type in ("Ma'ruza", "Amaliy"):
        query = query.filter_by(class_type=class_type)
    if date_str:
        try:
            att_date = date.fromisoformat(date_str)
            query = query.filter_by(date=att_date)
        except ValueError:
            return jsonify({'error': 'Invalid date format'}), 400

    records = query.order_by(Attendance.date.desc()).all()
    return jsonify([r.to_dict() for r in records]), 200


@app.route('/api/attendance/bulk', methods=['POST'])
@login_required(role=('teacher', 'admin'))
def save_bulk_attendance():
    data = request.get_json()
    subject_id = data.get('subject_id')
    class_type = (data.get('class_type') or "Ma'ruza").strip()
    records = data.get('records', [])

    if not subject_id:
        return jsonify({'error': "Avval fanni (Subject) tanlang!"}), 400

    if class_type not in ("Ma'ruza", "Amaliy"):
        return jsonify({'error': "Mashg'ulot turi 'Ma'ruza' yoki 'Amaliy' bo'lishi kerak!"}), 400

    subj = Subject.query.get_or_404(subject_id)
    if session.get('role') == 'teacher':
        allowed_subjs = get_teacher_subjects(session.get('full_name', ''), session.get('user_id'))
        if subj.id not in {s.id for s in allowed_subjs}:
            return jsonify({'error': "Siz ushbu fanga biriktirilmagansiz!"}), 403

    try:
        att_date = date.fromisoformat(data['date']) if data.get('date') else date.today()
    except ValueError:
        return jsonify({'error': "Sana formati noto'g'ri"}), 400

    saved = 0
    for item in records:
        student_id = item.get('student_id')
        status = item.get('status', 'absent')
        note = (item.get('note') or '').strip()

        if not student_id or status not in ('present', 'absent', 'late'):
            continue

        existing = Attendance.query.filter_by(
            student_id=student_id,
            subject_id=subject_id,
            class_type=class_type,
            date=att_date
        ).first()

        if existing:
            existing.status = status
            existing.note = note
        else:
            db.session.add(Attendance(
                student_id=student_id,
                subject_id=subject_id,
                class_type=class_type,
                date=att_date,
                status=status,
                note=note
            ))
        saved += 1

    db.session.commit()
    return jsonify({'message': f"{subj.localized_name} ({class_type}) bo'yicha {saved} ta talaba davomati saqlandi!"}), 200


# ==================== SMART QR ATTENDANCE ==================== #

@app.route('/api/qr/start', methods=['POST'])
@login_required(role=('teacher', 'admin'))
def start_qr_session():
    data = request.get_json()
    subject_id = data.get('subject_id')
    class_type = (data.get('class_type') or "Ma'ruza").strip()
    date_str = data.get('date') or date.today().isoformat()

    if not subject_id:
        return jsonify({'error': "Darsni boshlash uchun avval fanni tanlang!"}), 400

    if class_type not in ("Ma'ruza", "Amaliy"):
        return jsonify({'error': "Mashg'ulot turini ('Ma'ruza' yoki 'Amaliy') tanlang!"}), 400

    subject = Subject.query.get_or_404(subject_id)
    teacher_name = subject.lecture_teacher if class_type == "Ma'ruza" else subject.practical_teacher

    token = secrets.token_urlsafe(12)
    ACTIVE_QR_SESSIONS[token] = {
        'subject_id': subject.id,
        'subject_name': subject.localized_name,
        'class_type': class_type,
        'subject_code': subject.code,
        'teacher_name': teacher_name,
        'date': date_str,
        'teacher_id': session['user_id'],
        'checked_in_students': []
    }

    checkin_url = url_for('qr_checkin_page', token=token, _external=True)
    return jsonify({
        'token': token,
        'checkin_url': checkin_url,
        'subject_name': subject.localized_name,
        'localized_name': subject.localized_name,
        'class_type': class_type,
        'subject_code': subject.code,
        'teacher_name': teacher_name,
        'date': date_str
    }), 200


@app.route('/api/qr/status/<token>', methods=['GET'])
@login_required(role=('teacher', 'admin'))
def qr_session_status(token):
    info = ACTIVE_QR_SESSIONS.get(token)
    if not info:
        return jsonify({'error': 'Session not found'}), 404
    return jsonify(info), 200


@app.route('/qr-checkin/<token>', methods=['GET', 'POST'])
def qr_checkin_page(token):
    info = ACTIVE_QR_SESSIONS.get(token)
    if not info:
        return "<h3>QR kod muddati tugagan yoki noto'g'ri!</h3>", 404

    att_date = date.fromisoformat(info['date'])

    if request.method == 'GET' and session.get('role') == 'student':
        student_id = session['user_id']
        student = User.query.get(student_id)
        if student:
            _mark_qr_present(student, info, att_date)
            return render_template(
                'qr_checkin.html',
                info=info,
                success_student=student,
                students=User.query.filter_by(role='student').order_by(User.full_name).all()
            )

    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        student_id = data.get('student_id', type=int) if hasattr(data, 'get') else None
        username = (data.get('username') or '').strip()

        student = None
        if student_id:
            student = User.query.filter_by(id=student_id, role='student').first()
        elif username:
            student = User.query.filter_by(username=username, role='student').first()
        elif session.get('role') == 'student':
            student = User.query.get(session['user_id'])

        if not student:
            if request.is_json:
                return jsonify({'error': 'Talaba topilmadi!'}), 404
            return render_template(
                'qr_checkin.html',
                info=info,
                error="Talaba topilmadi!",
                students=User.query.filter_by(role='student').order_by(User.full_name).all()
            )

        _mark_qr_present(student, info, att_date)
        if request.is_json:
            return jsonify({
                'message': f"{student.full_name} uchun '{info['subject_name']} ({info['class_type']})' darsidan davomat belgilandi!",
                'student_name': student.full_name
            }), 200

        return render_template(
            'qr_checkin.html',
            info=info,
            success_student=student,
            students=User.query.filter_by(role='student').order_by(User.full_name).all()
        )

    students = User.query.filter_by(role='student').order_by(User.full_name).all()
    return render_template('qr_checkin.html', info=info, students=students)


def _mark_qr_present(student, info, att_date):
    existing = Attendance.query.filter_by(
        student_id=student.id,
        subject_id=info['subject_id'],
        class_type=info['class_type'],
        date=att_date
    ).first()
    if existing:
        existing.status = 'present'
        existing.note = f"QR Code ({info['class_type']})"
    else:
        db.session.add(Attendance(
            student_id=student.id,
            subject_id=info['subject_id'],
            class_type=info['class_type'],
            date=att_date,
            status='present',
            note=f"QR Code ({info['class_type']})"
        ))
    db.session.commit()
    if student.full_name not in info['checked_in_students']:
        info['checked_in_students'].append(student.full_name)


# ==================== EXPORT CSV / EXCEL (HEMIS 36-CLASS MODEL) ==================== #

@app.route('/api/attendance/export', methods=['GET'])
@login_required(role=('teacher', 'admin'))
def export_csv():
    subject_id = request.args.get('subject_id', type=int)
    class_type = request.args.get('class_type') or "Ma'ruza"
    date_str = request.args.get('date')
    group_name = request.args.get('group_name')

    try:
        att_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        att_date = date.today()

    st_query = User.query.filter_by(role='student')
    if group_name:
        st_query = st_query.filter_by(group_name=group_name)
    students = st_query.order_by(User.group_name.asc(), User.full_name.asc()).all()

    teacher_subjects_list = get_teacher_subjects(session.get('full_name', ''), session.get('user_id'))
    subject = Subject.query.get(subject_id) if subject_id else (teacher_subjects_list[0] if teacher_subjects_list else None)

    att_map = {}
    if subject:
        records = Attendance.query.filter_by(
            subject_id=subject.id,
            class_type=class_type,
            date=att_date
        ).all()
        att_map = {r.student_id: r for r in records}

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')
    writer.writerow([
        '#', 'Talaba F.I.Sh', 'Username', 'Guruh',
        'Fan', 'Mashg\'ulot Turi', 'O\'qituvchi', 'Sana',
        'Bugungi Holat', 'Qoldirilgan Darslar (36 dan)', 'Qoldirilgan Foiz (Hemis %)', 'Red Line Status', 'Izoh'
    ])

    for idx, st in enumerate(students, start=1):
        subj_stats = calculate_student_stats(st.id, subject.id if subject else None)
        rec = att_map.get(st.id)
        status_label = 'Keldi' if (rec and rec.status == 'present') else ('Kelmadi' if rec else 'Keldi (0% qoldirish)')
        teacher_for_type = (
            subject.lecture_teacher if class_type == "Ma'ruza" else subject.practical_teacher
        ) if subject else 'N/A'

        writer.writerow([
            idx,
            st.full_name,
            st.username,
            st.group_name or DEFAULT_GROUP,
            subject.localized_name if subject else 'N/A',
            class_type,
            teacher_for_type,
            att_date.isoformat(),
            status_label,
            f"{subj_stats['absent_count']} / 36",
            f"{subj_stats['missed_percentage']}%",
            'RED LINE (>25%)' if subj_stats['is_red_line'] else 'Normal',
            rec.note if rec else ''
        ])

    filename = f"ATT241_Hemis_{subject.code if subject else 'fan'}_{class_type}_{att_date.isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


# ==================== STUDENT DASHBOARD API (HEMIS 36-CLASS MODEL) ==================== #

@app.route('/api/student/me', methods=['GET'])
@login_required(role=('student', 'admin'))
def student_me_data():
    student = User.query.get_or_404(session['user_id'])
    if student.role != 'student':
        student = User.query.filter_by(role='student').first_or_404()

    overall_stats = calculate_student_stats(student.id)

    # Filter subjects belonging to the student's group via N:M (fallback to all subjects)
    grp = Group.query.filter_by(name=student.group_name or DEFAULT_GROUP).first()
    subjects = grp.subjects if (grp and grp.subjects) else Subject.query.order_by(Subject.id.asc()).all()

    subject_breakdown = []
    any_subject_red_line = False
    for subj in subjects:
        merged_stats = calculate_student_stats(student.id, subj.id)
        if merged_stats['is_red_line']:
            any_subject_red_line = True
        subject_breakdown.append({
            'subject_id': subj.id,
            'subject_code': subj.code,
            'subject_name': subj.localized_name,
            'localized_name': subj.localized_name,
            'lecture_teacher': subj.lecture_teacher,
            'practical_teacher': subj.practical_teacher,
            'stats': merged_stats
        })

    if any_subject_red_line:
        overall_stats['is_red_line'] = True

    active_sessions = []
    for token, info in ACTIVE_QR_SESSIONS.items():
        qr_subj = Subject.query.get(info.get('subject_id'))
        session_copy = {'token': token, **info}
        if qr_subj:
            session_copy['subject_name'] = qr_subj.localized_name
            session_copy['localized_name'] = qr_subj.localized_name
        active_sessions.append(session_copy)

    return jsonify({
        'student': student.to_dict(),
        'major_name': MAJOR_NAME,
        'default_group': DEFAULT_GROUP,
        'overall_stats': overall_stats,
        'subject_breakdown': subject_breakdown,
        'weekly_schedule': get_grouped_weekly_schedule(),
        'active_qr_sessions': active_sessions
    }), 200


if __name__ == '__main__':
    app.run(debug=True)
