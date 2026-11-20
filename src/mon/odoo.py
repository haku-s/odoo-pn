
    def check_fields(self, write: bool = False) -> dict:
        if write:
            self.cfg.require_odoo(write=True)

        task = self.fields('project.task')
        project = self.fields('project.project')

        def valid(
            spec: dict,
            name: str,
            types: set,
            label: str,
        ):
            if not name or name not in spec:
                raise ValueError(
                    f'{label}: required field is missing'
                )

            meta = spec[name]

            if (
                meta.get('type') not in types
                or (write and meta.get('readonly'))
            ):
                raise ValueError(
                    f'{label}: incompatible field configuration'
                )

        if write:
            reserved = (
                self.cfg.pncp_field,
                self.cfg.checkpoint_field,
            )

            if (
                any(
                    not field.startswith('x_')
                    for field in reserved
                )
                or len(set(reserved)) != 2
                or any(
                    field in self.cfg.field_map.values()
                    for field in reserved
                )
            ):
                raise ValueError(
                    'Unsafe reserved field configuration'
                )

            valid(
                task,
                self.cfg.pncp_field,
                FIELD_TYPES['pncp'],
                'identifier',
            )

            valid(
                project,
                self.cfg.checkpoint_field,
                FIELD_TYPES['checkpoint'],
                'checkpoint',
            )

            if 'project_id' not in task or 'name' not in task:
                raise ValueError(
                    'Required base fields are missing'
                )

            if self.cfg.stage_id and 'stage_id' not in task:
                raise ValueError(
                    'Required stage field is missing'
                )

            for source, dest in self.cfg.field_map.items():
                if source not in ALLOWED_FIELDS:
                    raise ValueError(
                        'Unsupported source field'
                    )

                valid(
                    task,
                    dest,
                    FIELD_TYPES[source],
                    source,
                )

        return {
            'project.task': task,
            'project.project': project,
        }
