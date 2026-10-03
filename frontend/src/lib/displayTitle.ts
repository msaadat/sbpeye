/*
 * Title case for titles SBP publishes in capitals (docs/REDESIGN_PLAN.md FN8).
 *
 * Display only. The stored title is untouched, so search, identity and anything that
 * compares against sbp.org.pk still see SBP's text; this changes what a reader sees and
 * nothing else. `AGENTS.md` § Known issues on the SBP site records the exception.
 *
 * A title is converted only when it is shouting: under 15% of its letters lowercase. On
 * 2026-10-03 that was 568 of 3,788 circular and law titles, and no ordinary mixed-case
 * title comes near the threshold. Within a shouting title, a word keeps its capitals when it
 * is an acronym — a known one (below), or one with no vowels, which in this corpus is
 * always an acronym (SBP, BPRD, PLS, MMTS) — a Roman numeral, a dotted abbreviation
 * (A.H.), or anything with an ampersand or a digit. A word SBP already cased (DFIs, eCIB)
 * is left as written.
 */

// Acronyms that contain a vowel, so the vowel rule cannot catch them. Drawn from the
// all-capital titles in the corpus plus the regulatory vocabulary around them.
const ACRONYMS = new Set([
  'ACFID', 'ADB', 'AML', 'ATM', 'BCO', 'CEO', 'CFT', 'CIB', 'CNIC', 'COVID', 'CPF', 'DAP',
  'DFI', 'ECIB', 'EPD', 'EPZ', 'FATF', 'FE', 'FX', 'GOP', 'IAS', 'IBAN', 'IFPD', 'IFRS',
  'IMF', 'IT', 'JIAP', 'KIBOR', 'MFI', 'NADRA', 'NBFI', 'NBI', 'NIFT', 'OMO', 'PIB', 'POL',
  'POS', 'PRISM', 'SECP', 'SME', 'UAE', 'UK', 'US', 'USA', 'USD',
])

// Words with no A, E, I, O or U that are words, not acronyms.
const VOWELLESS_WORDS = new Set(['BY', 'MY', 'DRY', 'TRY', 'WHY', 'FLY', 'SKY', 'SHY', 'MR', 'MRS', 'DR'])

// Lowercase except as the first word, or the first after a colon, dash or bracket.
// `ul` is the Arabic article in names like Ramadan-ul-Mubarak.
const SMALL_WORDS = new Set([
  'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'into', 'nor', 'of',
  'on', 'or', 'per', 'the', 'to', 'ul', 'via', 'vs', 'with',
])

// "A" after one of these is a label ("Schedule A"), not the article.
const LABELS = new Set([
  'ANNEX', 'ANNEXURE', 'APPENDIX', 'CATEGORY', 'CHAPTER', 'CLASS', 'CLAUSE', 'FORM', 'GRADE',
  'GROUP', 'LIST', 'OPTION', 'PARA', 'PART', 'PHASE', 'PLAN', 'SCHEDULE', 'SECTION', 'TIER',
  'TYPE',
])

const ROMAN = /^(?=[IVX])X{0,3}(?:IX|IV|V?I{0,3})$/
const ORDINAL = /^(\d+)(ST|ND|RD|TH)$/
const POSSESSIVE = /^(.+?)(['’])S$/
const SEPARATOR = /^[\s\-–—/()[\]]+$/

function isShouting(text: string): boolean {
  const letters = text.match(/\p{L}/gu) ?? []
  if (letters.length < 4) return false
  const lower = letters.filter(ch => ch !== ch.toUpperCase()).length
  return lower / letters.length < 0.15
}

function isAcronym(word: string): boolean {
  if (ACRONYMS.has(word)) return true
  if (VOWELLESS_WORDS.has(word)) return false
  return word.length >= 2 && !/[AEIOU]/.test(word)
}

function capitalise(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
}

/** One word: a run of letters, digits and inner punctuation, no spaces, hyphens or slashes. */
function caseWord(word: string, startsClause: boolean): string {
  if (!word) return word
  // Dotted: case each part, so A.H. and H.O.T. keep their letters while NO.101 and
  // U.S.DOLLARS become No.101 and U.S.Dollars.
  if (word.includes('.')) {
    return word.split('.').map((part, i) => caseWord(part, startsClause && i === 0)).join('.')
  }
  // Already cased by SBP (DFIs, MFBs, eCIB) — except a long capital stem with a stray
  // lowercase plural, which is a typo ("OPERATIONs") rather than an acronym.
  if (/[a-z]/.test(word)) {
    const stray = word.match(/^([A-Z]{2,})s$/)
    if (stray && !isAcronym(stray[1])) return capitalise(word)
    return word
  }
  if (/\d/.test(word)) {
    const ordinal = word.match(ORDINAL)
    if (ordinal) return ordinal[1] + ordinal[2].toLowerCase()
    const prefixed = word.match(/^(\p{L}+)(\d.*)$/u)
    return prefixed ? caseWord(prefixed[1], startsClause) + prefixed[2] : word
  }
  if (word.includes('&')) return word
  const possessive = word.match(POSSESSIVE)
  if (possessive) return caseWord(possessive[1], startsClause) + possessive[2] + 's'
  if (word.length === 1 || ROMAN.test(word) || isAcronym(word)) return word
  const lower = word.toLowerCase()
  if (!startsClause && SMALL_WORDS.has(lower)) return lower
  return capitalise(word)
}

export function displayTitle(title?: string | null): string {
  if (!title) return title ?? ''
  if (!isShouting(title)) return title
  // Words and the separators between them, both kept. A compound's parts are separate
  // words, so FE-CIRCULAR and RAMADAN-UL-MUBARAK case part by part.
  const tokens = title.match(/[^\s\-–—/()[\]]+|[\s\-–—/()[\]]+/g) ?? [title]
  const isWord = (i: number) => i >= 0 && i < tokens.length && !SEPARATOR.test(tokens[i])
  let startsClause = true
  let previousWord = ''
  return tokens.map((token, i) => {
    if (!isWord(i)) {
      if (/[–—(\[]/.test(token) || /\s-\s/.test(token)) startsClause = true
      return token
    }
    const match = token.match(/^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}.&'’]*)$/u)
    const [, lead, core, trail] = match ?? ['', '', token, '']
    let cased: string
    if (core === 'A' && !startsClause && tokens.slice(i + 1).some((_, j) => isWord(i + 1 + j))
        && !LABELS.has(previousWord)) {
      cased = 'a'
    } else {
      cased = caseWord(core, startsClause)
    }
    previousWord = core.toUpperCase()
    startsClause = /[:;]$/.test(trail) || /[:;]$/.test(core)
    return lead + cased + trail
  }).join('')
}
