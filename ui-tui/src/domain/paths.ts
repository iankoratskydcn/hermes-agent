export const shortCwd = (cwd: string, max = 28) => {
  const h = process.env.HOME
  const p = h && cwd.startsWith(h) ? `~${cwd.slice(h.length)}` : cwd

  return p.length <= max ? p : `…${p.slice(-(max - 1))}`
}

export const fmtCwdBranch = (cwd: string, branch: null | string, max = 40) => {
  if (!branch) {
    return shortCwd(cwd, max)
  }

  const tag = ` (${branch.length > 16 ? `…${branch.slice(-15)}` : branch})`

  return `${shortCwd(cwd, Math.max(8, max - tag.length))}${tag}`
}

export const shortProject = (projectName: string, max = 18) => {
  const name = projectName.trim()

  return name.length <= max ? name : `${name.slice(0, Math.max(1, max - 1))}…`
}

// Status-bar workspace label: the terminal has no hover tooltip, so the project
// name is shown INLINE alongside the cwd/branch (`<project> · ~/cwd (branch)`).
// Falls back to the plain cwd/branch label when the session sits in no named
// project, and when space is tight the project name wins (it's the identity the
// user recognizes) with the cwd/branch dropped.
export const fmtProjectCwdBranch = (cwd: string, branch: null | string, projectName?: null | string, max = 40) => {
  const project = shortProject(projectName || '')

  if (!project) {
    return fmtCwdBranch(cwd, branch, max)
  }

  const separator = ' · '
  const remaining = max - project.length - separator.length

  if (remaining < 8) {
    return shortProject(project, max)
  }

  return `${project}${separator}${fmtCwdBranch(cwd, branch, remaining)}`
}

/** Default cap on the session-name segment of a composed title. */
export const TITLE_MAX_NAME = 28

/** Default cap on the cwd segment of a composed title. */
export const TITLE_MAX_CWD = 24

export const shortSessionName = (sessionName: string, max = TITLE_MAX_NAME): string => {
  const name = sessionName.trim()

  return name.length > max ? `${name.slice(0, max - 1)}…` : name
}

/**
 * Compose the terminal titlebar string:
 *   `<marker> <session name> · <model> · <cwd>`
 *
 * The session name and cwd are each omitted when empty, and a long session
 * name is truncated. The marker is always glued to the first present segment
 * with a plain space (not a ` · ` separator). When no model is known yet the
 * caller should fall back to a plain brand string instead of calling this.
 */
export const composeTabTitle = (
  marker: string,
  sessionName: string,
  model: string,
  cwd: string,
  maxName = TITLE_MAX_NAME
): string => {
  const shortName = shortSessionName(sessionName, maxName)

  const segments = [shortName, model, cwd].filter(Boolean)

  return segments.length ? `${marker} ${segments.join(' · ')}` : marker
}

/** Values a title template can interpolate. `*_full` variants skip the
 *  length caps and prefix-stripping that the short forms apply. */
export interface TitleTokens {
  cwd: string
  cwdFull: string
  marker: string
  model: string
  modelFull: string
  session: string
  sessionFull: string
}

// Recognized placeholders. Unknown `{tokens}` are left VERBATIM rather than
// blanked: a typo stays visible in the title bar instead of silently
// vanishing, which is the cheaper failure to diagnose.
const TITLE_TOKEN_PATTERN = /\{(cwd|cwd_full|marker|model|model_full|session|session_full)\}/g

/**
 * Render a user-supplied title template (`display.tab_title_template` /
 * `display.window_title_template`).
 *
 * Empty/blank template ⇒ `null`, meaning "caller keeps its built-in default".
 *
 * Segment separators are part of the template, so a token that resolves to an
 * empty string would leave a dangling ` · `. We therefore collapse runs of
 * separator/whitespace left by empty tokens, and trim them from both ends.
 */
export const renderTitleTemplate = (template: string, tokens: TitleTokens): null | string => {
  if (!template.trim()) {
    return null
  }

  const substituted = template.replace(TITLE_TOKEN_PATTERN, (_match, name: string) => {
    switch (name) {
      case 'cwd':
        return tokens.cwd

      case 'cwd_full':
        return tokens.cwdFull

      case 'marker':
        return tokens.marker

      case 'model':
        return tokens.model

      case 'model_full':
        return tokens.modelFull

      case 'session':
        return tokens.session

      case 'session_full':
        return tokens.sessionFull

      default:
        return ''
    }
  })

  // Collapse the separators orphaned by empty tokens (`a ·  · b` ⇒ `a · b`),
  // then strip leading/trailing separators and whitespace.
  return substituted
    .replace(/\s*·(?:\s*·)+\s*/g, ' · ')
    .replace(/^[\s·]+|[\s·]+$/g, '')
    .replace(/\s{2,}/g, ' ')
}
