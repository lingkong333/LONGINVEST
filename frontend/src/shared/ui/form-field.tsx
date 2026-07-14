import * as React from "react"
import {
  useController,
  type Control,
  type ControllerRenderProps,
  type FieldPath,
  type FieldValues,
} from "react-hook-form"

import { Field, FieldDescription, FieldError, FieldLabel } from "@/shared/ui/field"

interface FormFieldRenderProps<
  Values extends FieldValues,
  Name extends FieldPath<Values>,
> {
  field: ControllerRenderProps<Values, Name>
  invalid: boolean
}

interface FormFieldProps<
  Values extends FieldValues,
  Name extends FieldPath<Values>,
> {
  control: Control<Values>
  name: Name
  label: string
  description?: string
  children: (
    props: FormFieldRenderProps<Values, Name>,
  ) => React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>
}

export function FormField<
  Values extends FieldValues,
  Name extends FieldPath<Values>,
>({
  control,
  name,
  label,
  description,
  children,
}: FormFieldProps<Values, Name>) {
  const { field, fieldState } = useController({ control, name })
  const generatedId = React.useId()
  const controlId = `${generatedId}-control`
  const descriptionId = description ? `${generatedId}-description` : undefined
  const error = fieldState.error?.message
  const errorId = error ? `${generatedId}-error` : undefined
  const renderedControl = children({ field, invalid: fieldState.invalid })
  const describedBy = [renderedControl.props["aria-describedby"], descriptionId, errorId]
    .filter(Boolean)
    .join(" ") || undefined

  return (
    <Field data-invalid={fieldState.invalid}>
      <FieldLabel htmlFor={controlId}>{label}</FieldLabel>
      {React.cloneElement(renderedControl, {
        id: controlId,
        "aria-describedby": describedBy,
        "aria-invalid": fieldState.invalid,
      })}
      {description ? <FieldDescription id={descriptionId}>{description}</FieldDescription> : null}
      {error ? <FieldError id={errorId}>{error}</FieldError> : null}
    </Field>
  )
}
