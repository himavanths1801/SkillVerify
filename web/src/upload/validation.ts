export const MAX_FILE_BYTES = 5 * 1024 * 1024;
export const ALLOWED_EXTENSIONS = [".pdf", ".docx", ".txt"] as const;

export const FILE_TYPE_MESSAGE = "That file type isn't supported";
export const FILE_SIZE_MESSAGE = "That file is larger than 5 MB";
export const USERNAME_MESSAGE = "That GitHub username doesn't look right";

/** The error text for a chosen file, or null when it is fine. */
export function validateFile(file: Pick<File, "name" | "size">): string | null {
  const dot = file.name.lastIndexOf(".");
  const extension = dot === -1 ? "" : file.name.slice(dot).toLowerCase();
  if (!(ALLOWED_EXTENSIONS as readonly string[]).includes(extension)) return FILE_TYPE_MESSAGE;
  if (file.size > MAX_FILE_BYTES) return FILE_SIZE_MESSAGE;
  return null;
}

/** Letters, digits and hyphens, 1 to 39 characters, not starting or ending with a hyphen. Empty is fine (optional). */
export function validateUsername(value: string): string | null {
  const name = value.trim();
  if (name === "") return null;
  return /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/.test(name) ? null : USERNAME_MESSAGE;
}

export function formatKb(bytes: number): string {
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export type UploadInputs = {
  jdMode: "file" | "text";
  jdFile: File | null;
  jdText: string;
  cvFile: File | null;
  username: string;
};

export function hasJobDescription(inputs: Pick<UploadInputs, "jdMode" | "jdFile" | "jdText">): boolean {
  return inputs.jdMode === "file" ? inputs.jdFile !== null : inputs.jdText.trim() !== "";
}

/** Exactly one of jd_file / jd_text, plus cv_file, plus github_username only when it is not empty. */
export function buildFormData(inputs: UploadInputs): FormData {
  const form = new FormData();
  if (inputs.jdMode === "file" && inputs.jdFile) form.append("jd_file", inputs.jdFile);
  else form.append("jd_text", inputs.jdText);
  if (inputs.cvFile) form.append("cv_file", inputs.cvFile);
  const username = inputs.username.trim();
  if (username !== "") form.append("github_username", username);
  return form;
}
