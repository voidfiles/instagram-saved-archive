/** Browser-safe mapping shared by static and dynamically rendered media. */
export function assetUrl(
  path: string,
  base = import.meta.env.BASE_URL,
): string {
  if (
    /[\\:\0]/.test(path) ||
    path.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    throw new Error("Invalid local asset path");
  }
  const prefix = base.replace(/\/+$/, "");
  return `${prefix}/archive/${path.split("/").map(encodeURIComponent).join("/")}`;
}
