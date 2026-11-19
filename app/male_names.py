"""Мужские имена и ники: в канал SisterFit (и любой канал сестёр) мужчинам нельзя.

looks_male(first, last, username) → причина (строка) или "" если не похоже на мужчину.
Сомнительные имена (Саша, Женя, Нур…) не трогаем — лучше пропустить, чем отказать сестре.
Если в имени есть явно женское имя (Амина, Aisha…) — тоже пропускаем.
"""
from __future__ import annotations

import re

_MALE = """
александр алексей анатолий андрей антон аркадий арсений артем артур богдан борис вадим валерий
василий виктор виталий владимир владислав всеволод вячеслав геннадий георгий глеб григорий давид
даниил данил данила денис дмитрий евгений егор иван игорь илья кирилл константин лев леонид максим
марк матвей михаил никита николай олег павел петр роман ростислав руслан семен сергей станислав
степан тимофей тимур федор филипп эдуард юрий ярослав дима вова володя костя миша паша петя сережа
серега толя леха леша вася гоша гриша рома витя ваня димон макс
абдулла абдуллах абдурахман абдулазиз абдулла абдурашид абдул абу абубакр адам адиль азамат азиз айдар
акбар алан алихан али алим алишер альберт амир амин анвар ансар арман арслан аслан асхат ахмад ахмед
ахмет ахмат байрам батыр билал бекзат бахтияр вагиф вали гамзат гусейн давлат далер джамал джамшид
джафар джавид дауд даниял ерлан жамал жасур закир заур зелимхан ибрагим идрис ильдар ильяс ильнур
ильшат имам иса ислам исмаил исхак камил карим касым курбан магомед магомет мага махмуд мансур марат
мурат муса мустафа мухаммад мухаммед мухамед мухаммадали мурад нурлан нурсултан осман равиль рамазан
рамиль рашид ренат ризван рустам рустем саид салават салим салман самир санжар сардор сулейман
султан тагир тахир умар усман фарид фаррух хабиб хамза хасан хусейн хусан шамиль шамиль шерзод
шахзод эльдар эмиль юнус юсуф якуб ясин азат айрат булат динар ильгиз ринат рафаэль
alexander alexandr alexey aleksey andrey andrei anton artem artyom artur boris denis dmitry dmitriy
dima egor evgeny evgeniy igor ivan ilya kirill konstantin maksim maxim max mikhail misha nikita
nikolay oleg pavel roman ruslan sergey sergei stanislav timur vadim viktor victor vladimir
vladislav vova yuri yury yaroslav
abdullah abdulla abdul abdurahman abu abubakr adam adil ahmad ahmed ahmet akhmed ali alikhan alisher
amir amin anvar arman aslan aziz bilal daud dawud farid farukh hamza hasan hassan husein hussein
ibrahim ibragim idris ilyas imran isa islam ismail ishaq jamal jamshid kamil karim khabib
khasan magomed mahmud mansur marat murat murad musa mustafa muhammad muhammed mohammed mohamed
mohammad muhammet nurlan omar osman ramazan rashid rustam said salim salman samir sardor shamil
sulayman suleiman sultan tahir umar usman yunus yusuf yakub yasin zaur
john james michael david daniel thomas robert william alex mike tom ben jack peter paul mark
муж брат ахи akhi brother bro husband
"""
_FEMALE = """
амина аиша айша асия асият фатима фатимат хадижа хадиджа марьям мариям мадина малика зарема залина
зульфия зухра лейла лейля камила карина сабина сафия зейнаб зайнаб хава хафса сумайя самира диана
алина анна мария марина елена ольга наталья татьяна ирина светлана юлия екатерина анастасия дарья
ксения софия виктория полина элина эльвира гульнара гузель айгуль аяна умм уммы сестра сестренка
amina aisha aysha asiya fatima fatimah khadija hadija maryam mariam madina malika zarema zulfiya
zukhra leila leyla layla kamila karina sabina safiya zaynab zainab hava hafsa sumaya samira diana
alina anna maria marina elena olga natalia tatiana irina svetlana yulia ekaterina anastasia daria
ksenia sofia victoria polina elina umm ukhti sister sis
"""
MALE = set(_MALE.split())
FEMALE = set(_FEMALE.split())
# Мужская форма фамилии/отчества: Иванов, Алиев, Пушкин, Петрович, Ахмедович, …-оглы, …-улы.
_MALE_SURNAME = re.compile(r"^[а-я]{2,}(ов|ев|ин|ын|ский|цкий|вич|ович|евич|оглы|улы|уулу)$")


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _words(text: str | None) -> list[str]:
    """Имя/ник → слова: «Ahmed_Aliev95», «ahmedAliev» → [ahmed, aliev]."""
    text = re.sub(r"([a-zа-я])([A-ZА-Я])", r"\1 \2", text or "")   # camelCase
    return [w for w in re.split(r"[^a-zа-яё]+", _norm(text)) if w]


def looks_male(first_name: str | None, last_name: str | None = None, username: str | None = None) -> str:
    names = _words(first_name) + _words(last_name)
    nick = _words(username)
    words = names + nick
    if any(w in FEMALE for w in words):
        return ""
    for w in words:
        if w in MALE:
            return f"мужское имя «{w}»"
    for w in _words(last_name) + _words(first_name)[1:]:
        if _MALE_SURNAME.match(w):
            return f"мужская фамилия «{w}»"
    return ""
