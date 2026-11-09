variable "PYTHON_IMAGE" { default = "platform/python314:ubuntu26.04-20261109-r2" }
variable "CS_VIEWER_IMAGE_TAG" { default = "ubuntu26.04-20261110-r7" }
group "default" { targets = ["cs-viewer"] }
target "cs-viewer" {
  context = "."
  dockerfile = "Dockerfile"
  platforms = ["linux/arm64"]
  args = { PYTHON_IMAGE = PYTHON_IMAGE }
  tags = ["platform/cs-viewer:${CS_VIEWER_IMAGE_TAG}"]
}
