import * as React from "react"

import { Field, FieldDescription, FieldError, FieldLabel } from "@/shared/ui/field"

interface FormFieldProps {
  label: string
  htmlFor: string
  description?: string
  error?: string
  children: React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>
}

export function FormField({
  label,
  htmlFor,
  description,
  error,
  children,
}: FormFieldProps) {
  const generatedId = React.useId()
  const descriptionId = description ? `${generatedId}-description` : undefined
  const errorId = error ? `${generatedId}-error` : undefined
  const describedBy = [children.props["aria-describedby"], descriptionId, errorId]
    .filter(Boolean)
    .join(" ") || undefined

  return (
    <Field data-invalid={Boolean(error)}>
      <FieldLabel htmlFor={htmlFor}>{label}</FieldLabel>
      {React.cloneElement(children, { "aria-describedby": describedBy })}
      {description ? <FieldDescription id={descriptionId}>{description}</FieldDescription> : null}
      {error ? <FieldError id={errorId}>{error}</FieldError> : null}
    </Field>
  )
}
